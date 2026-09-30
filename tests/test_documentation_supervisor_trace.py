"""Namespace-init cleanup cannot hide an interrupted workload descendant."""

import pytest

from scripts.documentation_command_runner import _NetworkTrace

pytestmark = pytest.mark.unit

_LAUNCH = '100 execve("/usr/bin/bwrap", [], []) = 0\n'
_CLONE = "100 clone(child_stack=NULL, flags=CLONE_NEWUSER|CLONE_NEWPID|SIGCHLD) = 101\n"
_OWNER_EXIT = '102 execve("/run/python", [], []) = 0\n102 exit_group(7) = ?\n'
_HELPER_KILL = "101 +++ killed by SIGKILL +++\n"


@pytest.mark.parametrize("chunk_size", [1, 23, 4096])
@pytest.mark.parametrize("owner_status", [0, 7])
def test_completed_namespace_init_cleanup_is_not_workload_interruption(chunk_size: int, owner_status: int) -> None:
    trace = _NetworkTrace("/run/python")
    data = (
        _LAUNCH
        + _CLONE
        + _OWNER_EXIT.replace("exit_group(7)", f"exit_group({owner_status})")
        + "103 exit_group(0) = ?\n100 exit_group(7) = ?\n"
        + _HELPER_KILL
    ).encode()
    for index in range(0, len(data), chunk_size):
        trace.consume(data[index : index + chunk_size])
    trace.finish()

    assert trace.interrupted_descendant is False
    assert trace.attempted is False


@pytest.mark.parametrize("chunk_size", [1, 23, 4096])
@pytest.mark.parametrize("exit_entry", ["exit_group(0) = ?", "exit_group(0 <unfinished ...>"])
def test_namespace_init_exit_entry_is_not_terminal_retirement(chunk_size: int, exit_entry: str) -> None:
    trace = _NetworkTrace("/run/python")
    data = (
        _LAUNCH
        + _CLONE
        + _OWNER_EXIT.replace("exit_group(7)", "exit_group(0)")
        + "100 exit_group(0) = ?\n"
        + f"101 {exit_entry}\n"
        + _HELPER_KILL
    ).encode()
    for index in range(0, len(data), chunk_size):
        trace.consume(data[index : index + chunk_size])
    trace.finish()

    assert trace.interrupted_descendant is False
    assert trace.attempted is False


@pytest.mark.parametrize(
    "clone",
    [
        "100 clone(flags=CLONE_NEWPID|SIGCHLD) = -1 EPERM\n",
        "100 clone(flags=CLONE_NEWPID|SIGCHLD) = 0\n",
        "100 clone(flags=CLONE_NEWUSER|SIGCHLD) = 101\n",
        "100 clone(flags=FAKE_CLONE_NEWPID|SIGCHLD) = 101\n",
        '100 clone(flags="CLONE_NEWPID"|SIGCHLD) = 101\n',
        '100 clone(flags=SIGCHLD, comment="CLONE_NEWPID") = 101\n',
        "100 clone(flags=CLONE_NEWPID;SIGCHLD) = 101\n",
        "100 <... clone resumed>) = 101\n",
    ],
)
def test_unknown_namespace_clone_never_exempts_a_killed_process(clone: str) -> None:
    trace = _NetworkTrace("/run/python")
    trace.consume((_LAUNCH + clone + _OWNER_EXIT + _HELPER_KILL).encode())
    assert trace.interrupted_descendant is True


@pytest.mark.parametrize(
    "retirement",
    [
        "101 +++ exited with 0 +++\n",
        '101 execve("/usr/bin/bwrap", [], []) = 0\n',
        '101 execve("/missing", [], []) = -1 ENOENT\n',
        "101 +++ killed by SIGTERM +++\n",
        _HELPER_KILL,
    ],
)
def test_namespace_init_exemption_does_not_survive_pid_retirement(retirement: str) -> None:
    trace = _NetworkTrace("/run/python")
    trace.consume((_LAUNCH + _CLONE + _OWNER_EXIT + retirement + _HELPER_KILL).encode())
    assert trace.interrupted_descendant is True


def test_later_workload_bwrap_cannot_select_an_exempt_namespace_init() -> None:
    trace = _NetworkTrace("/run/python")
    trace.consume(
        (
            _LAUNCH
            + _CLONE
            + _OWNER_EXIT
            + "100 exit_group(7) = ?\n"
            + '103 execve("/usr/bin/bwrap", [], []) = 0\n'
            + "103 clone(flags=CLONE_NEWPID|SIGCHLD) = 104\n"
            + "104 +++ killed by SIGKILL +++\n"
        ).encode()
    )
    assert trace.interrupted_descendant is True


def test_namespace_init_cleanup_does_not_hide_real_descendant_kill() -> None:
    trace = _NetworkTrace("/run/python")
    trace.consume(
        (
            _LAUNCH
            + _CLONE
            + _OWNER_EXIT
            + _HELPER_KILL
            + '103 execve("/usr/bin/bwrap", [], []) = 0\n'
            + "103 +++ killed by SIGKILL +++\n"
        ).encode()
    )
    assert trace.interrupted_descendant is True


def test_namespace_init_network_attempt_remains_rejected() -> None:
    trace = _NetworkTrace("/run/python")
    trace.consume(
        (
            _LAUNCH
            + _CLONE
            + _OWNER_EXIT
            + '101 connect(3, {sa_family=AF_INET, sin_addr=inet_addr("198.51.100.1")}, 16) = -1 ENETUNREACH\n'
            + _HELPER_KILL
        ).encode()
    )
    assert trace.attempted is True


def test_successful_clone3_selects_only_original_namespace_init() -> None:
    trace = _NetworkTrace("/run/python")
    trace.consume(
        (
            _LAUNCH
            + "100 clone3({flags=CLONE_NEWPID|CLONE_NEWUSER, exit_signal=SIGCHLD}, 88) = 101\n"
            + _OWNER_EXIT
            + _HELPER_KILL
        ).encode()
    )
    assert trace.interrupted_descendant is False


@pytest.mark.parametrize(
    "launch", ["", '100 execve("/missing", [], []) = -1 ENOENT\n', '100 execve("/other", [], []) = 0\n' + _LAUNCH]
)
def test_namespace_clone_without_original_trusted_launcher_cannot_exempt(launch: str) -> None:
    trace = _NetworkTrace("/run/python")
    trace.consume((launch + _CLONE + _OWNER_EXIT + _HELPER_KILL).encode())
    assert trace.interrupted_descendant is True
