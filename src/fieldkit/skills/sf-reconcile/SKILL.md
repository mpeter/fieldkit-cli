---
name: sf-reconcile
description: >
  Pursuit-vs-Salesforce drift detection across your active pipeline. Fetches live SF for
  every active pursuit with a linked Opportunity, flags divergence from the pursuit files
  (stage, close date, consulting ACV, closing window) RED/YELLOW/GREEN, and syncs only on approval.
  Use when the operator types /sf-reconcile or asks whether pursuit files match Salesforce,
  including before a pipeline review or forecast call — even if they only say "are my files
  up to date" or "check the deals".
metadata:
  opencode/slash: "true"
  category: product
---

# /sf-reconcile — do the pursuit files still match Salesforce?

SF is deal-truth; the pursuit files are a working snapshot that drifts. This
skill pulls live SF for every active, SF-linked pursuit, flags every
divergence, and **changes nothing without the operator's say-so**. It is a
report, not an autosync.

Not to be confused with `fieldkit sf reconcile`, which rewrites the Key
Fields table in a *single* file. This skill is portfolio-wide drift
detection.

Scope is derived, not hand-maintained: every pursuit `fieldkit pursuit health`
reports as active that carries an `sf_opportunity_id`. There is no separate
watchlist file to keep in sync.

## Step 1 — preflight

```bash
fieldkit sf session-check
```

Exit 0: proceed. Exit 2: stop and tell the operator to run `fieldkit auth sf`
first. Do not present cached data as current — a failed fetch is a RED flag
for that opportunity, not a skip.

## Step 2 — collect (read-only, never mutates a pursuit file)

Run the whole pass as one script so the comparison is deterministic rather
than eyeballed. `uv` supplies PyYAML, so the operator's system Python needs
nothing installed:

```bash
mkdir -p scratch/out
uv run --no-project --with pyyaml python - <<'PY'
import json, re, subprocess, sys
from datetime import UTC, datetime
from pathlib import Path
import yaml

ACCOUNT = None            # "<slug>" narrows to one account
INCLUDE_PROSPECT = False  # True widens to prospect-stage pursuits
LIFECYCLE = {"prospect", "qualify", "discover", "validate", "propose", "negotiate", "closed-won", "closed-lost"}
TODAY = datetime.now(tz=UTC).date()

def money(v):
    s = re.sub(r"[^\d.\-]", "", str(v or ""))
    try:
        return round(float(s))
    except ValueError:
        return None

def stage(s):
    s = str(s or "").strip().lower()
    return s.replace("closed ", "closed-") if s.startswith("closed ") else s

def closed(s):
    return s.startswith("closed")

def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---[ \t]*(\n|$)", text, re.S)
    fm = yaml.safe_load(m.group(1)) if m else {}
    if not isinstance(fm, dict):
        raise ValueError("frontmatter is not a mapping")
    return fm

def days_until(d):
    try:
        return (datetime.fromisoformat(str(d)[:10]).date() - TODAY).days
    except ValueError:
        return None

def run_json(cmd, timeout):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[:160] or f"exit {p.returncode}")
    return json.loads(p.stdout)

def reconcile(opp_id, path):
    flags = []
    try:
        fm = frontmatter(Path(path))
    except OSError as exc:
        return {"id": opp_id, "pursuit": path, "flags": [("RED", "missing-file", str(exc))]}
    except (ValueError, yaml.YAMLError) as exc:
        return {"id": opp_id, "pursuit": path, "flags": [("RED", "bad-frontmatter", str(exc)[:160])]}
    try:
        live = run_json(["fieldkit", "sf", "opportunity", opp_id, path, "--no-write", "--json"], 90)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"id": opp_id, "pursuit": path, "flags": [("RED", "sf-fetch-failed", str(exc)[:160])]}

    local, sf, stored = stage(fm.get("stage")), stage(live.get("stage")), stage(fm.get("sf_stage"))
    if sf in LIFECYCLE and local and local != sf:
        flags.append(("YELLOW", "stage-mismatch", f"local '{local}' != SF '{sf}'"))
    if sf and stored != sf:
        flags.append(("YELLOW", "sf-stage-drift", f"stored sf_stage '{stored}' != live '{sf}'"))
    if live.get("close_date") and str(fm.get("sf_close_date") or "")[:10] != live["close_date"][:10]:
        flags.append(("YELLOW", "close-date-drift", f"stored {fm.get('sf_close_date') or '—'} != live {live['close_date']}"))
    stored_acv, live_acv = money(fm.get("sf_consulting_acv")) or 0, money(live.get("consulting_acv")) or 0
    if stored_acv != live_acv:  # blank and $0 are equal; a cleared SF value still drifts
        flags.append(("YELLOW", "acv-drift", f"stored {fm.get('sf_consulting_acv')} != live {live.get('consulting_acv')}"))
    du = days_until(live.get("close_date"))
    if du is not None and not closed(sf):
        if du < 0:
            flags.append(("RED", "overdue", f"close date {-du}d past, still open"))
        elif du <= 14:
            flags.append(("RED", "closing-14d", f"closes in {du}d"))
        elif du <= 30:
            flags.append(("YELLOW", "closing-30d", f"closes in {du}d"))
    if closed(sf) and not closed(local):
        flags.append(("RED", "sf-closed-local-open", f"SF '{sf}', local '{local}'"))
    return {"id": opp_id, "pursuit": path, "name": live.get("name"), "flags": flags,
            "live": {"stage": live.get("stage"), "close_date": live.get("close_date"), "consulting_acv": live.get("consulting_acv")}}

if not Path("accounts").is_dir():
    sys.exit("Run from the fieldkit workspace root (the directory containing accounts/).")
cmd = ["fieldkit", "pursuit", "health", "--json"]
cmd += ["--account", ACCOUNT] if ACCOUNT else []
cmd += ["--include-prospect"] if INCLUDE_PROSPECT else []
try:
    # exit 1 = incomplete assessment: valid rows on stdout, damaged files named on stderr
    h = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if h.returncode not in (0, 1):
        raise RuntimeError(h.stderr.strip()[:160] or f"exit {h.returncode}")
    rows = [r for r in json.loads(h.stdout) if r.get("sf_opportunity_id")]
except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
    sys.exit(f"No pursuits in scope: {exc}")
unassessed = [l.removeprefix("WARNING: ") for l in h.stderr.splitlines() if "assessment incomplete" in l]
for line in unassessed:
    print(f"NOT ASSESSED {line}", file=sys.stderr)
results = []
for r in rows:
    # health reports paths relative to accounts/
    e = reconcile(r["sf_opportunity_id"], f"accounts/{r['relative_path']}")
    levels = {f[0] for f in e["flags"]}
    e["status"] = "RED" if "RED" in levels else "YELLOW" if levels else "GREEN"
    results.append(e)
    print(f"{e['status']:6} {r['relative_path']}: " + (", ".join(f[1] for f in e["flags"]) or "clean"), file=sys.stderr)

report = {"count": len(results), "red": sum(e["status"] == "RED" for e in results),
          "yellow": sum(e["status"] == "YELLOW" for e in results), "unassessed": unassessed,
          "opportunities": results}
Path("scratch/out/sf-reconcile.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(f"\n{report['red']} RED, {report['yellow']} YELLOW of {report['count']}, {len(unassessed)} not assessed"
      " -> scratch/out/sf-reconcile.json", file=sys.stderr)
PY
```

Run it from the workspace root. Edit `ACCOUNT` or `INCLUDE_PROSPECT` at the
top of the script to narrow or widen the scope. Each opportunity is a live
fetch with a 90-second limit, so a large portfolio can outlast an agent's
default command timeout: run it in the background, or narrow with
`ACCOUNT`.

`stage-mismatch` compares only when the SF stage name is one of fieldkit's
lifecycle stages. Organizations with their own stage names still get
`sf-stage-drift`, which compares SF against the stored `sf_stage` snapshot.

## Step 3 — present the drift report (RED/YELLOW/GREEN)

- 🔴 **RED** — `overdue` (SF close date passed, still open), `closing-14d`,
  `sf-closed-local-open` (SF closed, local still open), `sf-fetch-failed`,
  `missing-file`, `bad-frontmatter`.
- 🟡 **YELLOW** — `stage-mismatch` (local vs SF), `sf-stage-drift` /
  `close-date-drift` / `acv-drift` (consulting ACV; file snapshot stale vs live),
  `closing-30d`.
- 🟢 **GREEN** — clean.

Show every opportunity in scope, including rows with missing data, and lead
with RED. A pursuit with no `sf_opportunity_id` is out of scope for this
report, not GREEN — list it separately as "not yet linked to SF" when the
operator asks about pipeline completeness. Pre-pipeline pursuits are never
in scope.

**Lead with any `unassessed` entries.** These are pursuit files
`pursuit health` could not read (malformed YAML, missing frontmatter, bad
encoding), so their opportunities were not checked at all. A report with
unassessed files is incomplete, however clean the rest looks; name each
file and suggest `fieldkit pursuit audit` to diagnose it.

## Step 4 — act only on approval (flag, don't fix)

Propose, then wait:

- **Snapshot drift** (`sf-*-drift`, `close-date-drift`, `acv-drift`) → offer
  to sync that pursuit's `sf_*` frontmatter with
  `fieldkit sf opportunity <id> accounts/<relative_path>` (no `--no-write`).
  One pursuit at a time, on the operator's word.
- **stage-mismatch** → surface it and ask; local `stage` is the operator's
  judgment (see the `pursuit-advance` skill for gate checks).
- **overdue / sf-closed-local-open** → surface for a close/slip decision; SF
  and local stage stay as they are until the operator decides.
- **Qualification questions** → route to the `grill` skill, which reads
  native ClosePlan evidence. This skill does not judge qualification.

## Gotchas

- **A `sf-fetch-failed` flag means the fetch died, not that the opportunity
  is clean.** A run with fetch failures is incomplete; say so instead of
  summarizing it as "mostly clean."
- **Not the same as `fieldkit sf reconcile`** — that rewrites Key Fields in a
  single file; this skill is portfolio-wide drift detection.

## Constraints

- **Read-only by default** — the only writes are `scratch/out/sf-reconcile.json`
  and `sf_*` frontmatter via `fieldkit sf opportunity`, and only after the
  operator approves a specific pursuit.
- **Leave local `stage` to the operator** — surface mismatches and ask.
- **One pursuit at a time on approval** — batch syncs need explicit
  per-pursuit confirmation.

## Related skills

- `pipeline` — local-only staleness/risk scan; this skill adds the live-SF
  comparison.
- `sf-sync` — the sync half of this workflow; applies the fix this skill
  flags.
- `grill` — native ClosePlan qualification review.
