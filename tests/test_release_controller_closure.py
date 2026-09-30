"""Adversarial contracts for preparatory controller closure comparison."""

import hashlib
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from functools import partial
from pathlib import Path

import pytest

from scripts import release_controller_closure as closure
from scripts import release_filesystem

pytestmark = pytest.mark.unit
_SUBPROCESS_TIMEOUT_SECONDS = 20


def _member(path: str, data: bytes) -> closure.ControllerMember:
    return closure.ControllerMember(path, len(data), hashlib.sha256(data).hexdigest())


@pytest.fixture
def controller(tmp_path: Path) -> tuple[Path, tuple[closure.ControllerMember, ...]]:
    root = tmp_path / "controller"
    (root / "lib").mkdir(parents=True)
    (root / "main.py").write_bytes(b"raise RuntimeError('must not execute')\n")
    (root / "lib" / "helper.py").write_bytes(b"VALUE = 1\n")
    members = tuple(_member(path, (root / path).read_bytes()) for path in ("main.py", "lib/helper.py"))
    return root, members


def test_closed_controller_returns_no_release_status(
    controller: tuple[Path, tuple[closure.ControllerMember, ...]],
) -> None:
    root, members = controller
    verify: Callable[[], object] = partial(
        closure.verify_controller_closure, root, expected_entrypoint="main.py", expected_members=members
    )
    result = verify()
    assert result is None


@pytest.mark.parametrize("change", ["mutation", "removal"])
def test_capture_retains_immutable_bytes_after_source_change(
    controller: tuple[Path, tuple[closure.ControllerMember, ...]], change: str
) -> None:
    root, members = controller
    expected = closure.ControllerCapture(
        "main.py", (("lib/helper.py", b"VALUE = 1\n"), ("main.py", b"raise RuntimeError('must not execute')\n"))
    )
    result = closure.capture_controller_closure(root, expected_entrypoint="main.py", expected_members=members)
    assert result == expected
    for member in members:
        if change == "mutation":
            (root / member.path).write_bytes(b"changed after capture")
        else:
            (root / member.path).unlink()
    assert result == expected


@pytest.mark.parametrize("attribute", ["entrypoint", "members"])
def test_capture_fields_are_frozen(
    controller: tuple[Path, tuple[closure.ControllerMember, ...]], attribute: str
) -> None:
    root, members = controller
    result = closure.capture_controller_closure(root, expected_entrypoint="main.py", expected_members=members)
    assert isinstance(result, closure.ControllerCapture)
    with pytest.raises(FrozenInstanceError, match=attribute):
        setattr(result, attribute, None)


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("entrypoint", "../main.py", "path"),
        ("entrypoint", "missing.py", "entrypoint"),
        ("members", (), "count"),
        ("members", [("main.py", b"")], "count"),
        ("members", (("main.py", b"", b"extra"),), "member is invalid"),
        ("members", (("../main.py", b""),), "path"),
        ("members", (("/main.py", b""),), "path"),
        ("members", (("lib//main.py", b""),), "path"),
        ("members", (("x" * 4097, b""),), "path"),
        ("members", (("/".join(["x"] * 33), b""),), "path"),
        ("members", (("main.py", b""), ("main.py", b"")), "duplicate"),
        ("members", (("main.py", b""), ("main.py/helper.py", b"")), "directory"),
        ("members", (("main.py", bytearray(b"mutable")),), "immutable bytes"),
        ("members", (("main.py", "text"),), "immutable bytes"),
    ],
)
def test_validator_rejects_forged_capture_without_opening_sources(
    monkeypatch: pytest.MonkeyPatch, attribute: str, value: object, message: str
) -> None:
    capture = closure.ControllerCapture("main.py", (("main.py", b""),))
    object.__setattr__(capture, attribute, value)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid capture must not allocate files or open a parent")

    monkeypatch.setattr(release_filesystem, "open_real_directory", forbidden)
    with pytest.raises(ValueError, match=message):
        closure.validate_controller_capture(capture)


@pytest.mark.parametrize("bound", ["count", "bytes"])
def test_validator_enforces_capture_bounds(monkeypatch: pytest.MonkeyPatch, bound: str) -> None:
    monkeypatch.setattr(closure, "MAX_CONTROLLER_MEMBERS", 2)
    monkeypatch.setattr(closure, "MAX_CONTROLLER_BYTES", 3)
    members = (
        (("main.py", b"ab"), ("x", b"cd")) if bound == "bytes" else tuple((name, b"") for name in ("main.py", "x", "y"))
    )
    capture = closure.ControllerCapture("main.py", members)
    with pytest.raises(ValueError, match=r"count|total bytes"):
        closure.validate_controller_capture(capture)


def test_capture_uses_validated_first_pass_bytes_once(
    controller: tuple[Path, tuple[closure.ControllerMember, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, members = controller
    original = release_filesystem.read_regular_file
    reads: list[bytes] = []

    def record_read(descriptor: int, *, maximum_bytes: int) -> bytes:
        data = original(descriptor, maximum_bytes=maximum_bytes)
        reads.append(data)
        return data

    monkeypatch.setattr(release_filesystem, "read_regular_file", record_read)
    result = closure.capture_controller_closure(root, expected_entrypoint="main.py", expected_members=members)
    assert result == closure.ControllerCapture(
        "main.py", (("lib/helper.py", b"VALUE = 1\n"), ("main.py", b"raise RuntimeError('must not execute')\n"))
    )
    assert len(reads) == len(members)
    assert all(any(data is read for read in reads) for _, data in result.members)


@pytest.mark.parametrize("change", ["mutation", "removal", "extra", "file_symlink", "directory_symlink"])
def test_capture_rejects_changes_between_scans_without_partial_return(
    controller: tuple[Path, tuple[closure.ControllerMember, ...]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    root, members = controller
    initial = root.stat()
    original_scan = os.scandir
    original_capture = closure.ControllerCapture
    root_scans = 0
    captures = 0

    def record_capture(entrypoint: str, captured_members: tuple[tuple[str, bytes], ...]) -> closure.ControllerCapture:
        nonlocal captures
        captures += 1
        return original_capture(entrypoint, captured_members)

    @contextmanager
    def change_between_scans(descriptor: int) -> Iterator[Iterator[os.DirEntry[str]]]:
        nonlocal root_scans
        is_root = os.fstat(descriptor).st_ino == initial.st_ino
        if is_root:
            root_scans += 1
        with original_scan(descriptor) as entries:
            yield entries
        if is_root and root_scans == 1:
            target = root / "lib" / "helper.py"
            if change == "mutation":
                target.write_bytes(b"VALUE = 2\n")
            elif change == "removal":
                target.unlink()
            elif change == "extra":
                (root / "lib" / "extra.py").write_bytes(b"extra")
            else:
                target = root / "lib" if change == "directory_symlink" else target
                retained = tmp_path / "retained"
                target.rename(retained)
                target.symlink_to(retained, target_is_directory=change == "directory_symlink")

    monkeypatch.setattr(os, "scandir", change_between_scans)
    monkeypatch.setattr(closure, "ControllerCapture", record_capture)
    with pytest.raises((ValueError, OSError), match=r"changed|unexpected|regular|directory|symbolic"):
        closure.capture_controller_closure(root, expected_entrypoint="main.py", expected_members=members)
    assert captures == 0


def test_capture_rejects_mutation_during_read(
    controller: tuple[Path, tuple[closure.ControllerMember, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, members = controller
    original = release_filesystem.read_regular_file

    def mutate(descriptor: int, *, maximum_bytes: int) -> bytes:
        data = original(descriptor, maximum_bytes=maximum_bytes)
        inode = os.fstat(descriptor).st_ino
        target = next(root / member.path for member in members if (root / member.path).stat().st_ino == inode)
        target.write_bytes(b"changed during read")
        return data

    monkeypatch.setattr(release_filesystem, "read_regular_file", mutate)
    with pytest.raises(ValueError, match="changed"):
        closure.capture_controller_closure(root, expected_entrypoint="main.py", expected_members=members)


@pytest.mark.parametrize("scan_number", [1, 2])
@pytest.mark.parametrize("change", ["extra", "transient"])
def test_capture_rejects_nested_change_at_end_of_scan(
    controller: tuple[Path, tuple[closure.ControllerMember, ...]],
    monkeypatch: pytest.MonkeyPatch,
    scan_number: int,
    change: str,
) -> None:
    root, members = controller
    nested = root / "lib"
    initial = nested.stat()
    root_initial = root.stat()
    original_scan = os.scandir
    nested_scans = 0

    @contextmanager
    def mutate_after_nested_scan(descriptor: int) -> Iterator[Iterator[os.DirEntry[str]]]:
        nonlocal nested_scans
        is_nested = os.fstat(descriptor).st_ino == initial.st_ino
        if is_nested:
            nested_scans += 1
        with original_scan(descriptor) as entries:
            yield entries
        if is_nested and nested_scans == scan_number:
            extra = nested / "extra.py"
            extra.write_bytes(b"unlisted")
            if change == "transient":
                extra.unlink()
            os.utime(nested, ns=(initial.st_atime_ns, initial.st_mtime_ns + 1_000_000_000))

    monkeypatch.setattr(os, "scandir", mutate_after_nested_scan)
    with pytest.raises(ValueError, match="directory changed during verification"):
        closure.capture_controller_closure(root, expected_entrypoint="main.py", expected_members=members)
    assert nested_scans == scan_number
    assert root.stat().st_mtime_ns == root_initial.st_mtime_ns
    assert root.stat().st_ctime_ns == root_initial.st_ctime_ns


@pytest.mark.parametrize("change", ["missing", "extra", "changed", "oversize", "empty_directory"])
def test_rejects_tree_mismatch(controller: tuple[Path, tuple[closure.ControllerMember, ...]], change: str) -> None:
    root, members = controller
    target = root / "lib" / "helper.py"
    if change == "missing":
        target.unlink()
    elif change == "extra":
        (root / "extra.py").write_bytes(b"extra")
    elif change == "changed":
        target.write_bytes(b"VALUE = 2\n")
    elif change == "oversize":
        target.write_bytes(b"too large" * 8)
    else:
        (root / "unexpected").mkdir()
    with pytest.raises(ValueError, match=r"missing|unexpected|digest|size"):
        closure.verify_controller_closure(root, expected_entrypoint="main.py", expected_members=members)


@pytest.mark.parametrize("kind", ["file", "directory", "root", "parent"])
def test_rejects_symlinks(
    controller: tuple[Path, tuple[closure.ControllerMember, ...]], tmp_path: Path, kind: str
) -> None:
    root, members = controller
    if kind == "file":
        target = root / "main.py"
        retained = tmp_path / "retained.py"
        target.rename(retained)
        target.symlink_to(retained)
    elif kind == "directory":
        target = root / "lib"
        retained = tmp_path / "retained"
        target.rename(retained)
        target.symlink_to(retained, target_is_directory=True)
    else:
        link = tmp_path / "link"
        link.symlink_to(root if kind == "root" else tmp_path, target_is_directory=True)
        root = link if kind == "root" else link / "controller"
    with pytest.raises((ValueError, OSError), match=r"regular|directory|symbolic"):
        closure.verify_controller_closure(root, expected_entrypoint="main.py", expected_members=members)


@pytest.mark.parametrize(
    "path",
    [
        "../main.py",
        "/main.py",
        "lib/../main.py",
        "./main.py",
        "lib//x",
        "x\\y",
        "x\x00y",
        "x:y",
        "x\ny",
        "é.py",
        "\ud800",
    ],
)
def test_rejects_noncanonical_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        closure.verify_controller_closure(tmp_path, expected_entrypoint=path, expected_members=[_member(path, b"")])


def test_rejects_duplicate_expectations(controller: tuple[Path, tuple[closure.ControllerMember, ...]]) -> None:
    root, members = controller
    with pytest.raises(ValueError, match="duplicate"):
        closure.verify_controller_closure(root, expected_entrypoint="main.py", expected_members=[*members, members[0]])


def test_rejects_absent_entrypoint(controller: tuple[Path, tuple[closure.ControllerMember, ...]]) -> None:
    root, members = controller
    with pytest.raises(ValueError, match="entrypoint"):
        closure.verify_controller_closure(root, expected_entrypoint="other.py", expected_members=members)


@pytest.mark.parametrize("size", [-1, True, closure.MAX_CONTROLLER_BYTES + 1])
def test_rejects_invalid_expected_size(tmp_path: Path, size: int) -> None:
    member = closure.ControllerMember("main.py", size, "0" * 64)
    with pytest.raises(ValueError, match="size"):
        closure.verify_controller_closure(tmp_path, expected_entrypoint="main.py", expected_members=[member])


def test_rejects_total_size_limit(tmp_path: Path) -> None:
    members = [closure.ControllerMember(name, closure.MAX_CONTROLLER_BYTES, "0" * 64) for name in ("main.py", "x")]
    with pytest.raises(ValueError, match="total bytes"):
        closure.verify_controller_closure(tmp_path, expected_entrypoint="main.py", expected_members=members)


@pytest.mark.parametrize("digest", ["", "f" * 63, "G" * 64, "0" * 65])
def test_rejects_invalid_digest(tmp_path: Path, digest: str) -> None:
    with pytest.raises(ValueError, match="digest"):
        closure.verify_controller_closure(
            tmp_path, expected_entrypoint="main.py", expected_members=[closure.ControllerMember("main.py", 0, digest)]
        )


@pytest.mark.parametrize("count", [0, closure.MAX_CONTROLLER_MEMBERS + 1])
def test_rejects_member_count_limit(tmp_path: Path, count: int) -> None:
    with pytest.raises(ValueError, match="count"):
        closure.verify_controller_closure(
            tmp_path, expected_entrypoint="main.py", expected_members=[_member("main.py", b"")] * count
        )


def test_rejects_nonregular_member(controller: tuple[Path, tuple[closure.ControllerMember, ...]]) -> None:
    root, members = controller
    target = root / "main.py"
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(ValueError, match="regular"):
        closure.verify_controller_closure(root, expected_entrypoint="main.py", expected_members=members)


def test_rejects_mutation_during_read(
    controller: tuple[Path, tuple[closure.ControllerMember, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, members = controller
    original = release_filesystem.read_regular_file

    def mutate(descriptor: int, *, maximum_bytes: int) -> bytes:
        data = original(descriptor, maximum_bytes=maximum_bytes)
        inode = os.fstat(descriptor).st_ino
        target = next(root / member.path for member in members if (root / member.path).stat().st_ino == inode)
        target.write_bytes(b"changed during read")
        return data

    monkeypatch.setattr(release_filesystem, "read_regular_file", mutate)
    with pytest.raises(ValueError, match="changed"):
        closure.verify_controller_closure(root, expected_entrypoint="main.py", expected_members=members)


def test_hostile_cwd_and_pythonpath_do_not_execute_controller(tmp_path: Path) -> None:
    root = tmp_path / "hostile"
    root.mkdir()
    sentinel = tmp_path / "sentinel"
    code = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
    paths = ("main.py", "release_filesystem.py", "sitecustomize.py")
    for path in paths:
        (root / path).write_text(code, encoding="utf-8")
    members = [_member(path, (root / path).read_bytes()) for path in paths]
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from pathlib import Path\n"
        "from scripts.release_controller_closure import ControllerMember, verify_controller_closure\n"
        f"members = [ControllerMember(*row) for row in {[(m.path, m.size, m.sha256) for m in members]!r}]\n"
        f"result = verify_controller_closure(Path({str(root)!r}), expected_entrypoint='main.py', expected_members=members)\n"
        "assert result is None\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()


@pytest.mark.parametrize("scan_number", [1, 2])
def test_rejects_transient_root_change(
    controller: tuple[Path, tuple[closure.ControllerMember, ...]], monkeypatch: pytest.MonkeyPatch, scan_number: int
) -> None:
    root, members = controller
    original = os.scandir
    initial = root.stat()
    root_scans = 0

    @contextmanager
    def transient_scan(descriptor: int) -> Iterator[Iterator[os.DirEntry[str]]]:
        nonlocal root_scans
        is_root = os.fstat(descriptor).st_ino == initial.st_ino
        if is_root:
            root_scans += 1
        with original(descriptor) as entries:
            yield entries
        if is_root and root_scans == scan_number:
            transient = root / "transient.py"
            transient.write_bytes(b"unlisted")
            transient.unlink()
            # Force a distinguishable directory timestamp even on coarse filesystems.
            os.utime(root, ns=(initial.st_atime_ns, initial.st_mtime_ns + 1_000_000_000))

    monkeypatch.setattr(os, "scandir", transient_scan)
    with pytest.raises(ValueError, match="changed during verification"):
        closure.verify_controller_closure(root, expected_entrypoint="main.py", expected_members=members)


def test_rejects_repeated_observed_path_before_reprocessing(
    controller: tuple[Path, tuple[closure.ControllerMember, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, members = controller
    original_scan = os.scandir
    original_read = release_filesystem.read_regular_file
    reads = 0

    @contextmanager
    def repeated_scan(descriptor: int) -> Iterator[Iterator[os.DirEntry[str]]]:
        with original_scan(descriptor) as entries:
            entry = next(entry for entry in entries if entry.name == "main.py")
            yield iter([entry, entry])

    def count_read(descriptor: int, *, maximum_bytes: int) -> bytes:
        nonlocal reads
        reads += 1
        return original_read(descriptor, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(os, "scandir", repeated_scan)
    monkeypatch.setattr(release_filesystem, "read_regular_file", count_read)
    with pytest.raises(ValueError, match="duplicate observed path"):
        closure.verify_controller_closure(root, expected_entrypoint="main.py", expected_members=members)
    assert reads == 1


def test_validator_returns_sorted_exact_retained_bytes() -> None:
    capture = closure.ControllerCapture("main.py", (("main.py", b"\x00"), ("empty", b"")))
    result = closure.validate_controller_capture(capture)
    assert result == (("empty", b""), ("main.py", b"\x00"))
