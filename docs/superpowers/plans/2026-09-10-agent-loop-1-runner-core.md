# Agent Loop — Plan 1 of 3: Runner Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deterministic Python runner that drives one bounded task through the full worker ladder (cheap ×2 → premium ×1 → blocked) against a fake worker, with every failure path from the spec's slice 2 proven by tests — no real Jira ticket, no real model, no real PR.

**Architecture:** Flat stdlib-only Python modules under `common/skills/agent-loop/scripts/`, one responsibility each (config, locks, admission, procs, contracts, worktree, agentdef, ladder, state, runner). The runner is a launchd-ticked CLI, not a daemon; all state is on disk under `~/.local/state/agent-loop/`. Workers are `pi -p` children launched from the *rendered* agent definitions in `~/.pi/agent/agents/`, so the agent file stays the single source of truth for model, thinking, tools, and system prompt.

**Tech Stack:** Python 3.14 (`/opt/homebrew/bin/python3`; `tomllib`, `fcntl`, `subprocess`, `unittest`), git, `pi` CLI, macOS `memory_pressure` / `pmset` / `vm_stat`, `flock` semantics via `fcntl.flock`.

**Spec:** `docs/superpowers/specs/2026-09-10-zip-6774-agent-loop-design.md` — sections *Substrate*, *Per-ticket lifecycle*, *Worker task contract*, *Escalation ladder*, *Resources and admission control*, *Error handling*, *Verification* slices 1–2. Plans 2 (ledger + dashboard) and 3 (gates, PR, review loops, AGENTS.md policy) follow this one and consume the interfaces it defines.

## Global Constraints

- Two repositories are touched. Agentfiles (`~/workspace/agentfiles`, `common/` is source of truth) gets everything new. The **live** `cmux_chain.py` / `spinup_helper.py` live in the dotfiles repo at `~/.config/lnk/Mac.lan.lnk/.claude/skills/spinup/scripts/` (git remote `crstamps2/dotfiles`); Task 12 edits *those*, not the sanitized `common/skills/spinup/` mirror. Commit to each repo separately; never push either without Cody.
- Runtime state root: `~/.local/state/agent-loop/` (`chmod 700`). Nothing under it is committed.
- Worker agent definitions declare **no** `fallbackModels`. Escalation is the runner's job.
- Workers may edit only `allowed_files`; test/fixture paths only when `may_edit_tests = true`; never `bin/`, `.github/`, CI config, `AGENTS.md`, or anything under the gate-script paths listed in `hopper.toml`.
- Heavy lane capacity 1, global; a local-model inference stage *is* a heavy stage. Light lane capacity 1 in the pilot.
- Locks are reclaimable only when the owner PID is dead or the boot ID differs — never on age.
- Every stage runs in its own process group; termination is verified before the lane is released.
- Run all agentfiles tests from `common/skills/agent-loop/scripts/` with `python3 -m unittest discover -p 'test_*.py' -v`. Run dotfiles tests from `~/.claude/skills/spinup/scripts/` with `python3 -m unittest test_cmux_chain test_spinup_helper -v`.
- `docs/superpowers/` is gitignored in agentfiles; plan and spec are force-added (`git add -f`) per the tracked-precedent convention.
- Never add `# frozen_string_literal` (Ruby rule, listed for completeness; no Ruby in this plan).

---

## File Structure

**Agentfiles — create:**

```
common/skills/agent-loop/
  SKILL.md                      operator doc: start/stop/pause/takeover/status
  hopper.toml                   epic, dependency graph, budgets, thresholds, paths, pre-registered outcomes
  inc.example.agent-loop.plist  launchd idle tick source (NOT loaded by this plan)
  scripts/
    config.py                   load hopper.toml → Config; state dir layout
    locks.py                    flock leases with PID/boot-id/heartbeat
    admission.py                machine-pressure probes → Admission decision
    procs.py                    process-group launch, timeout kill, verified termination
    contracts.py                result.md parser; tasks.toml validator
    worktree.py                 git snapshot/restore; changed paths; allowlist check
    agentdef.py                 rendered-agent-def loader → pi argv
    ladder.py                   arm assignment + ladder B sequencing + feedback.md
    state.py                    ticket state machine (JSON), PAUSE/HUMAN files
    metrics.py                  minimal append-only metrics.jsonl writer (Plan 2 extends)
    runner.py                   CLI: run-once, status, dry-run; orchestrates the above
    fake_worker.py              test double for `pi` — scenario-driven
    test_config.py … test_runner.py   one test module per module
common/agents/
  cloud-worker.agent.md
  local-worker.agent.md
  premium-worker.agent.md
```

**Agentfiles — modify:**

- `common/model-tiers.toml` — add `[worker-cloud]`, `[worker-local]`, `[worker-premium]`, `[flagship-author]`, `[flagship-critic]`.

**Dotfiles — modify (Task 12):**

- `~/.claude/skills/spinup/scripts/cmux_chain.py` — `run_chain(..., harness=None)` skips the agent tab; `cmd_cmux_poll` skips runner-owned epics.
- `~/.claude/skills/spinup/scripts/spinup_helper.py` — `list_assigned_eligible` returns `parent`.
- `~/.claude/skills/spinup/scripts/test_cmux_chain.py`, `test_spinup_helper.py` — tests.

**Live machine (Task 14, human-in-loop, no commit):** `~/.pi/agent/models.json` providers; Keychain item; Homebrew `ollama`; bootstrap render of the three worker agents.

---

### Task 1: Config loader and state layout

**Files:**
- Create: `common/skills/agent-loop/hopper.toml`
- Create: `common/skills/agent-loop/scripts/config.py`
- Test: `common/skills/agent-loop/scripts/test_config.py`

**Interfaces:**
- Produces: `config.load(path: Path | None = None) -> Config`; `Config` is a `dataclasses.dataclass` with fields `epic: str`, `owned_epics: list[str]`, `tickets: dict[str, TicketSpec]` (`TicketSpec(key, deps: list[str], pin_arm: str | None)`), `state_root: Path`, `cmux_chain_dir: Path`, `pi_agents_dir: Path`, `heavy_stage_timeout_s: int`, `admission: AdmissionThresholds`, `budgets: Budgets`, `protected_paths: list[str]`, `test_path_globs: list[str]`; `Config.ticket_dir(key) -> Path`; `Config.ensure_dirs() -> None`.
- `AdmissionThresholds(compressor_pct_max: float, load_per_core_max: float, disk_free_gb_min: float, require_ac_power: bool, defer_max_s: int)`.
- `Budgets(premium_daily_usd, premium_monthly_usd, total_unattended_daily_usd, cloud_starter_credit_usd, ci_rerun_max: int)`.

- [ ] **Step 1: Write `hopper.toml`**

```toml
# Agent loop configuration. Source of truth for what the runner may touch.
# Retrieval-dated values are labeled; re-check them when they age.

[hopper]
epic = "ZIP-6774"
owned_epics = ["ZIP-6774"]          # cmux listener skips children of these

# Dependency graph. Order here is also the tie-break order.
[[hopper.tickets]]
key = "ZIP-7872"   # Nav Link
[[hopper.tickets]]
key = "ZIP-7873"   # Well — pilot ticket
[[hopper.tickets]]
key = "ZIP-4293"   # Section
[[hopper.tickets]]
key = "ZIP-7877"   # Card Header Start
[[hopper.tickets]]
key = "ZIP-7876"   # Card Spotlight
[[hopper.tickets]]
key = "ZIP-4281"   # Nav Tabs
deps = ["ZIP-7872"]
[[hopper.tickets]]
key = "ZIP-4282"   # Nav Pills
deps = ["ZIP-7872"]
[[hopper.tickets]]
key = "ZIP-7875"   # Stepper
[[hopper.tickets]]
key = "ZIP-4294"   # Modal
[[hopper.tickets]]
key = "ZIP-7874"   # Dropzone

[paths]
state_root = "~/.local/state/agent-loop"
cmux_chain_dir = "~/.claude/skills/spinup/scripts"     # LIVE copy (dotfiles), not common/
pi_agents_dir = "~/.pi/agent/agents"

[timeouts]
heavy_stage_s = 4500        # 75 min, per field guide; calibrate in slice 4
light_stage_s = 1800

[admission]
compressor_pct_max = 25.0    # compressor-occupied pages / physical pages
load_per_core_max = 1.0
disk_free_gb_min = 20.0
require_ac_power = true
defer_max_s = 1800

[budgets]                    # USD; premium + total ENFORCE, others alert (Plan 2 wires alerts)
premium_daily_usd = 5.0
premium_monthly_usd = 40.0
total_unattended_daily_usd = 15.0
cloud_starter_credit_usd = 0.0   # fill from ollama.com usage page in slice 1
ci_rerun_max = 5

[protection]
# Never editable by workers, regardless of allowed_files.
protected_paths = ["bin/", ".github/", ".circleci/", "AGENTS.md", "CLAUDE.md", "config/ci/"]
# Editable only when the task manifest sets may_edit_tests = true.
test_path_globs = ["test/**", "spec/**", "**/fixtures/**", "**/__snapshots__/**"]

[arms]
# Cheap arms alternate by task index unless a ticket pins one. Values: cloud | local
alternate = ["cloud", "local"]

[outcomes]
# Pre-registered before the first real run; the dashboard shows these verbatim.
primary = "first-attempt acceptance rate per arm (cloud, local, premium)"
secondary = ["wall time per accepted task per arm", "escalation rate per arm", "estimated cost per accepted ticket", "human-wait time per approval class"]
```

- [ ] **Step 2: Write the failing test**

```python
# test_config.py
import os, pathlib, tempfile, unittest
import config

MINI = """
[hopper]
epic = "ZIP-1"
owned_epics = ["ZIP-1"]
[[hopper.tickets]]
key = "ZIP-10"
[[hopper.tickets]]
key = "ZIP-11"
deps = ["ZIP-10"]
pin_arm = "cloud"
[paths]
state_root = "{root}"
cmux_chain_dir = "/tmp/cc"
pi_agents_dir = "/tmp/agents"
[timeouts]
heavy_stage_s = 10
light_stage_s = 5
[admission]
compressor_pct_max = 25.0
load_per_core_max = 1.0
disk_free_gb_min = 20.0
require_ac_power = true
defer_max_s = 60
[budgets]
premium_daily_usd = 1.0
premium_monthly_usd = 2.0
total_unattended_daily_usd = 3.0
cloud_starter_credit_usd = 0.0
ci_rerun_max = 5
[protection]
protected_paths = ["bin/"]
test_path_globs = ["test/**"]
[arms]
alternate = ["cloud", "local"]
[outcomes]
primary = "p"
secondary = []
"""

class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "hopper.toml"
        self.path.write_text(MINI.format(root=self.tmp.name + "/state"))
    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_tickets_in_order_with_deps_and_pin(self):
        c = config.load(self.path)
        self.assertEqual(c.epic, "ZIP-1")
        self.assertEqual(list(c.tickets), ["ZIP-10", "ZIP-11"])
        self.assertEqual(c.tickets["ZIP-11"].deps, ["ZIP-10"])
        self.assertEqual(c.tickets["ZIP-11"].pin_arm, "cloud")
        self.assertIsNone(c.tickets["ZIP-10"].pin_arm)

    def test_paths_expand_user_and_ticket_dir(self):
        c = config.load(self.path)
        self.assertTrue(c.state_root.is_absolute())
        self.assertEqual(c.ticket_dir("ZIP-10"), c.state_root / "tickets" / "ZIP-10")

    def test_ensure_dirs_creates_layout_with_0700(self):
        c = config.load(self.path)
        c.ensure_dirs()
        for sub in ("tickets", "locks", "attempts"):
            self.assertTrue((c.state_root / sub).is_dir())
        self.assertEqual(oct(c.state_root.stat().st_mode & 0o777), "0o700")

    def test_default_path_is_hopper_toml_beside_scripts(self):
        self.assertEqual(config.DEFAULT_PATH.name, "hopper.toml")
        self.assertEqual(config.DEFAULT_PATH.parent.name, "agent-loop")

    def test_missing_required_table_raises(self):
        self.path.write_text("[hopper]\nepic='X'\n")
        with self.assertRaises(config.ConfigError):
            config.load(self.path)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd ~/workspace/agentfiles/common/skills/agent-loop/scripts && python3 -m unittest test_config -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'config'`

- [ ] **Step 4: Write `config.py`**

```python
"""Load hopper.toml into a typed Config and own the on-disk state layout."""
from __future__ import annotations
import dataclasses
import os
import pathlib
import tomllib

DEFAULT_PATH = pathlib.Path(__file__).resolve().parent.parent / "hopper.toml"
REQUIRED_TABLES = ("hopper", "paths", "timeouts", "admission", "budgets", "protection", "arms", "outcomes")


class ConfigError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class TicketSpec:
    key: str
    deps: list = dataclasses.field(default_factory=list)
    pin_arm: str | None = None


@dataclasses.dataclass(frozen=True)
class AdmissionThresholds:
    compressor_pct_max: float
    load_per_core_max: float
    disk_free_gb_min: float
    require_ac_power: bool
    defer_max_s: int


@dataclasses.dataclass(frozen=True)
class Budgets:
    premium_daily_usd: float
    premium_monthly_usd: float
    total_unattended_daily_usd: float
    cloud_starter_credit_usd: float
    ci_rerun_max: int


@dataclasses.dataclass(frozen=True)
class Config:
    epic: str
    owned_epics: list
    tickets: dict            # key -> TicketSpec, insertion-ordered
    state_root: pathlib.Path
    cmux_chain_dir: pathlib.Path
    pi_agents_dir: pathlib.Path
    heavy_stage_timeout_s: int
    light_stage_timeout_s: int
    admission: AdmissionThresholds
    budgets: Budgets
    protected_paths: list
    test_path_globs: list
    arms_alternate: list
    outcomes: dict

    def ticket_dir(self, key: str) -> pathlib.Path:
        return self.state_root / "tickets" / key

    def ensure_dirs(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_root, 0o700)
        for sub in ("tickets", "locks", "attempts"):
            (self.state_root / sub).mkdir(exist_ok=True)


def _p(s: str) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(s)).resolve()


def load(path: pathlib.Path | None = None) -> Config:
    path = pathlib.Path(path) if path else DEFAULT_PATH
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    missing = [t for t in REQUIRED_TABLES if t not in raw]
    if missing:
        raise ConfigError(f"{path}: missing tables {missing}")
    h, p, t, a, b, pr, arms, oc = (raw[k] for k in REQUIRED_TABLES)
    tickets = {}
    for row in h.get("tickets", []):
        spec = TicketSpec(key=row["key"], deps=list(row.get("deps", [])), pin_arm=row.get("pin_arm"))
        tickets[spec.key] = spec
    return Config(
        epic=h["epic"],
        owned_epics=list(h.get("owned_epics", [h["epic"]])),
        tickets=tickets,
        state_root=_p(p["state_root"]),
        cmux_chain_dir=_p(p["cmux_chain_dir"]),
        pi_agents_dir=_p(p["pi_agents_dir"]),
        heavy_stage_timeout_s=int(t["heavy_stage_s"]),
        light_stage_timeout_s=int(t["light_stage_s"]),
        admission=AdmissionThresholds(**a),
        budgets=Budgets(**b),
        protected_paths=list(pr["protected_paths"]),
        test_path_globs=list(pr["test_path_globs"]),
        arms_alternate=list(arms["alternate"]),
        outcomes=dict(oc),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m unittest test_config -v`
Expected: 5 tests PASS

- [ ] **Step 6: Commit**

```bash
cd ~/workspace/agentfiles
git add common/skills/agent-loop/hopper.toml common/skills/agent-loop/scripts/config.py common/skills/agent-loop/scripts/test_config.py
git commit -m "agent-loop: hopper.toml and typed config loader"
```

---

### Task 2: Leases with PID / boot-ID ownership

**Files:**
- Create: `common/skills/agent-loop/scripts/locks.py`
- Test: `common/skills/agent-loop/scripts/test_locks.py`

**Interfaces:**
- Produces: `locks.Lease(path: Path, name: str)` context manager; `.acquire(heartbeat_s: int = 60) -> bool`; `.release()`; `.heartbeat()`; module fn `locks.boot_id() -> str` (from `sysctl -n kern.bootsessionuuid`, falls back to `kern.boottime`); `locks.pid_alive(pid: int) -> bool`; `locks.owner(path) -> dict | None`.
- Semantics: `acquire` takes an exclusive `fcntl.flock` on `<path>.flock`, then reads `<path>` (JSON `{pid, boot_id, heartbeat_utc, name}`). If an owner record exists **and** its PID is alive **and** its boot_id equals ours, acquire returns `False`. Otherwise we write our record and return `True`. The flock is held only during the read-modify-write, so a crashed owner leaves a record but not an OS lock. Age is never consulted.

- [ ] **Step 1: Write the failing test**

```python
# test_locks.py
import json, os, pathlib, subprocess, sys, tempfile, unittest
import locks

class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "heavy"
    def tearDown(self):
        self.tmp.cleanup()

    def test_acquire_writes_owner_record(self):
        l = locks.Lease(self.path, "heavy")
        self.assertTrue(l.acquire())
        rec = locks.owner(self.path)
        self.assertEqual(rec["pid"], os.getpid())
        self.assertEqual(rec["boot_id"], locks.boot_id())
        self.assertEqual(rec["name"], "heavy")

    def test_second_acquire_by_live_owner_fails(self):
        a = locks.Lease(self.path, "heavy"); self.assertTrue(a.acquire())
        b = locks.Lease(self.path, "heavy"); self.assertFalse(b.acquire())

    def test_release_allows_reacquire(self):
        a = locks.Lease(self.path, "heavy"); a.acquire(); a.release()
        self.assertIsNone(locks.owner(self.path))
        self.assertTrue(locks.Lease(self.path, "heavy").acquire())

    def test_dead_pid_is_reclaimable(self):
        # spawn a child that exits immediately; its pid is dead by the time we read it
        child = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
        dead = int(child.stdout.strip())
        self.path.write_text(json.dumps({"pid": dead, "boot_id": locks.boot_id(), "heartbeat_utc": "2000-01-01T00:00:00Z", "name": "heavy"}))
        self.assertTrue(locks.Lease(self.path, "heavy").acquire())

    def test_live_pid_old_heartbeat_is_NOT_reclaimable(self):
        self.path.write_text(json.dumps({"pid": os.getpid(), "boot_id": locks.boot_id(), "heartbeat_utc": "2000-01-01T00:00:00Z", "name": "heavy"}))
        self.assertFalse(locks.Lease(self.path, "heavy").acquire())

    def test_different_boot_id_is_reclaimable_even_if_pid_alive(self):
        self.path.write_text(json.dumps({"pid": os.getpid(), "boot_id": "not-this-boot", "heartbeat_utc": "2999-01-01T00:00:00Z", "name": "heavy"}))
        self.assertTrue(locks.Lease(self.path, "heavy").acquire())

    def test_context_manager_releases(self):
        with locks.Lease(self.path, "heavy") as held:
            self.assertTrue(held)
        self.assertIsNone(locks.owner(self.path))

    def test_heartbeat_updates_timestamp(self):
        l = locks.Lease(self.path, "heavy"); l.acquire()
        t0 = locks.owner(self.path)["heartbeat_utc"]
        l.heartbeat()
        self.assertGreaterEqual(locks.owner(self.path)["heartbeat_utc"], t0)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_locks -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'locks'`

- [ ] **Step 3: Write `locks.py`**

```python
"""Leases keyed by PID + boot ID. Reclaim only when the owner is provably gone; never by age."""
from __future__ import annotations
import datetime as dt
import fcntl
import json
import os
import pathlib
import subprocess

_BOOT_ID = None


def boot_id() -> str:
    global _BOOT_ID
    if _BOOT_ID is None:
        for key in ("kern.bootsessionuuid", "kern.boottime"):
            r = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                _BOOT_ID = r.stdout.strip()
                break
        else:
            _BOOT_ID = "unknown-boot"
    return _BOOT_ID


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def owner(path: pathlib.Path) -> dict | None:
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Lease:
    def __init__(self, path: pathlib.Path, name: str):
        self.path = pathlib.Path(path)
        self.name = name
        self.held = False

    def _flock_path(self) -> pathlib.Path:
        return self.path.with_suffix(self.path.suffix + ".flock")

    def acquire(self, heartbeat_s: int = 60) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._flock_path(), "w") as fl:
            fcntl.flock(fl, fcntl.LOCK_EX)
            try:
                rec = owner(self.path)
                if rec and rec.get("boot_id") == boot_id() and pid_alive(int(rec.get("pid", -1))):
                    return False
                self._write()
                self.held = True
                return True
            finally:
                fcntl.flock(fl, fcntl.LOCK_UN)

    def _write(self) -> None:
        self.path.write_text(json.dumps({"pid": os.getpid(), "boot_id": boot_id(), "heartbeat_utc": _now(), "name": self.name}))

    def heartbeat(self) -> None:
        if self.held:
            self._write()

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.held = False

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_locks -v`
Expected: 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add common/skills/agent-loop/scripts/locks.py common/skills/agent-loop/scripts/test_locks.py
git commit -m "agent-loop: PID/boot-id leases, never age-reclaimed"
```

---

### Task 3: Admission control probes

**Files:**
- Create: `common/skills/agent-loop/scripts/admission.py`
- Test: `common/skills/agent-loop/scripts/test_admission.py`

**Interfaces:**
- Consumes: `config.AdmissionThresholds`.
- Produces: `admission.Reading(compressor_pct: float, load1: float, cores: int, on_ac: bool, therm_limited: bool, disk_free_gb: float)`; `admission.probe(state_root: Path) -> Reading` (runs `vm_stat`, `sysctl -n hw.ncpu hw.memsize`, `uptime`, `pmset -g batt`, `pmset -g therm`, `df -k`); `admission.decide(reading, thresholds) -> Decision(ok: bool, reasons: list[str])`; pure parsers `parse_vm_stat(text, memsize_bytes) -> float`, `parse_uptime(text) -> float`, `parse_batt(text) -> bool`, `parse_therm(text) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# test_admission.py
import unittest
from unittest.mock import patch
import admission
from config import AdmissionThresholds

VM = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               15804.
Pages active:                            471559.
Pages occupied by compressor:            407334.
Pageins:                                6490006.
"""
UPTIME = "11:30  up 20:58, 12 users, load averages: 2.17 2.35 2.49"
BATT_AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1234)\t100%; charged; 0:00 remaining present: true"
BATT_BAT = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1234)\t82%; discharging; 4:10 remaining present: true"
THERM_OK = "Note: No thermal warning level has been recorded\nCPU_Speed_Limit \t= 100\n"
THERM_HOT = "CPU_Speed_Limit \t= 62\n"
TH = AdmissionThresholds(compressor_pct_max=25.0, load_per_core_max=1.0, disk_free_gb_min=20.0, require_ac_power=True, defer_max_s=60)

class ParserTests(unittest.TestCase):
    def test_compressor_pct(self):
        memsize = 24 * 1024**3
        pct = admission.parse_vm_stat(VM, memsize)
        self.assertAlmostEqual(pct, 407334 * 16384 / memsize * 100, places=3)
    def test_load1(self):
        self.assertEqual(admission.parse_uptime(UPTIME), 2.17)
    def test_batt(self):
        self.assertTrue(admission.parse_batt(BATT_AC))
        self.assertFalse(admission.parse_batt(BATT_BAT))
    def test_therm(self):
        self.assertFalse(admission.parse_therm(THERM_OK))
        self.assertTrue(admission.parse_therm(THERM_HOT))
        self.assertFalse(admission.parse_therm(""))  # no data → not limited

class DecideTests(unittest.TestCase):
    def r(self, **kw):
        base = dict(compressor_pct=10.0, load1=2.0, cores=10, on_ac=True, therm_limited=False, disk_free_gb=100.0)
        base.update(kw)
        return admission.Reading(**base)
    def test_ok(self):
        d = admission.decide(self.r(), TH)
        self.assertTrue(d.ok); self.assertEqual(d.reasons, [])
    def test_each_red_reason(self):
        self.assertIn("compressor", admission.decide(self.r(compressor_pct=30.0), TH).reasons[0])
        self.assertIn("load", admission.decide(self.r(load1=11.0), TH).reasons[0])
        self.assertIn("battery", admission.decide(self.r(on_ac=False), TH).reasons[0])
        self.assertIn("thermal", admission.decide(self.r(therm_limited=True), TH).reasons[0])
        self.assertIn("disk", admission.decide(self.r(disk_free_gb=5.0), TH).reasons[0])
    def test_battery_ok_when_not_required(self):
        th = AdmissionThresholds(25.0, 1.0, 20.0, False, 60)
        self.assertTrue(admission.decide(self.r(on_ac=False), th).ok)

class ProbeTests(unittest.TestCase):
    @patch("admission.subprocess.run")
    def test_probe_assembles_reading(self, run):
        def fake(argv, **kw):
            out = {"vm_stat": VM, "uptime": UPTIME}.get(argv[0])
            if argv[:2] == ["pmset", "-g"] and argv[2] == "batt": out = BATT_AC
            if argv[:2] == ["pmset", "-g"] and argv[2] == "therm": out = THERM_OK
            if argv[0] == "sysctl" and "hw.ncpu" in argv: out = "10\n"
            if argv[0] == "sysctl" and "hw.memsize" in argv: out = str(24 * 1024**3) + "\n"
            if argv[0] == "df": out = "Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 100 50 52428800 50% /\n"
            m = unittest.mock.MagicMock(); m.stdout = out; m.returncode = 0; return m
        run.side_effect = fake
        r = admission.probe("/tmp")
        self.assertEqual(r.cores, 10); self.assertTrue(r.on_ac); self.assertAlmostEqual(r.disk_free_gb, 50.0, places=1)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_admission -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `admission.py`**

```python
"""Machine-pressure probes and the admission decision for heavy stages."""
from __future__ import annotations
import dataclasses
import re
import subprocess


@dataclasses.dataclass(frozen=True)
class Reading:
    compressor_pct: float
    load1: float
    cores: int
    on_ac: bool
    therm_limited: bool
    disk_free_gb: float


@dataclasses.dataclass(frozen=True)
class Decision:
    ok: bool
    reasons: list


def _out(argv: list) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def parse_vm_stat(text: str, memsize_bytes: int) -> float:
    page = re.search(r"page size of (\d+) bytes", text)
    comp = re.search(r"Pages occupied by compressor:\s+(\d+)", text)
    if not (page and comp and memsize_bytes):
        return 0.0
    return int(comp.group(1)) * int(page.group(1)) / memsize_bytes * 100.0


def parse_uptime(text: str) -> float:
    m = re.search(r"load averages?:\s*([\d.]+)", text)
    return float(m.group(1)) if m else 0.0


def parse_batt(text: str) -> bool:
    return "AC Power" in text


def parse_therm(text: str) -> bool:
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", text)
    return bool(m) and int(m.group(1)) < 100


def parse_df(text: str) -> float:
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return 0.0
    parts = lines[1].split()
    try:
        return int(parts[3]) / (1024 ** 2)   # 1024-blocks → GB
    except (IndexError, ValueError):
        return 0.0


def probe(state_root) -> Reading:
    memsize = int((_out(["sysctl", "-n", "hw.memsize"]) or "0").strip() or 0)
    cores = int((_out(["sysctl", "-n", "hw.ncpu"]) or "1").strip() or 1)
    return Reading(
        compressor_pct=parse_vm_stat(_out(["vm_stat"]), memsize),
        load1=parse_uptime(_out(["uptime"])),
        cores=cores,
        on_ac=parse_batt(_out(["pmset", "-g", "batt"])),
        therm_limited=parse_therm(_out(["pmset", "-g", "therm"])),
        disk_free_gb=parse_df(_out(["df", "-k", str(state_root)])),
    )


def decide(r: Reading, th) -> Decision:
    reasons = []
    if r.compressor_pct > th.compressor_pct_max:
        reasons.append(f"compressor {r.compressor_pct:.1f}% > {th.compressor_pct_max}%")
    if r.cores and r.load1 / r.cores > th.load_per_core_max:
        reasons.append(f"load {r.load1:.2f} on {r.cores} cores > {th.load_per_core_max}/core")
    if th.require_ac_power and not r.on_ac:
        reasons.append("on battery power")
    if r.therm_limited:
        reasons.append("thermal: CPU speed limited")
    if r.disk_free_gb < th.disk_free_gb_min:
        reasons.append(f"disk free {r.disk_free_gb:.1f}GB < {th.disk_free_gb_min}GB")
    return Decision(ok=not reasons, reasons=reasons)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_admission -v`
Expected: 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add common/skills/agent-loop/scripts/admission.py common/skills/agent-loop/scripts/test_admission.py
git commit -m "agent-loop: admission probes (memory, load, power, thermal, disk)"
```

---

### Task 4: Process-group stages with verified termination

**Files:**
- Create: `common/skills/agent-loop/scripts/procs.py`
- Test: `common/skills/agent-loop/scripts/test_procs.py`

**Interfaces:**
- Produces: `procs.run_stage(argv: list, cwd: Path, timeout_s: float, env: dict | None, stdout_path: Path, stderr_path: Path) -> StageResult(returncode: int | None, timed_out: bool, elapsed_s: float, pgid: int)`; `procs.kill_group(pgid: int, grace_s: float = 5.0) -> bool` (TERM, wait, KILL; returns True iff no member survives); `procs.group_alive(pgid) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# test_procs.py
import os, pathlib, sys, tempfile, time, unittest
import procs

class RunStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = pathlib.Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_success_captures_output_and_elapsed(self):
        r = procs.run_stage([sys.executable, "-c", "print('hi'); import sys; print('err', file=sys.stderr)"],
                            cwd=self.d, timeout_s=10, env=None,
                            stdout_path=self.d/"out", stderr_path=self.d/"err")
        self.assertEqual(r.returncode, 0); self.assertFalse(r.timed_out)
        self.assertEqual((self.d/"out").read_text().strip(), "hi")
        self.assertEqual((self.d/"err").read_text().strip(), "err")
        self.assertGreater(r.elapsed_s, 0)

    def test_timeout_kills_grandchildren_too(self):
        # child spawns a grandchild `sleep 60` that would outlive a naive kill
        code = "import subprocess,time; p=subprocess.Popen(['sleep','60']); open('gpid','w').write(str(p.pid)); time.sleep(60)"
        r = procs.run_stage([sys.executable, "-c", code], cwd=self.d, timeout_s=1.0, env=None,
                            stdout_path=self.d/"out", stderr_path=self.d/"err")
        self.assertTrue(r.timed_out)
        gpid = int((self.d/"gpid").read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(gpid, 0)
        self.assertFalse(procs.group_alive(r.pgid))

    def test_env_is_passed_and_cwd_honored(self):
        r = procs.run_stage([sys.executable, "-c", "import os; print(os.environ['AL_X']); print(os.getcwd())"],
                            cwd=self.d, timeout_s=5, env={**os.environ, "AL_X": "42"},
                            stdout_path=self.d/"out", stderr_path=self.d/"err")
        out = (self.d/"out").read_text().splitlines()
        self.assertEqual(out[0], "42"); self.assertEqual(pathlib.Path(out[1]).resolve(), self.d.resolve())

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_procs -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `procs.py`**

```python
"""Run a stage in its own process group; on timeout kill the whole group and verify."""
from __future__ import annotations
import dataclasses
import os
import pathlib
import signal
import subprocess
import time


@dataclasses.dataclass(frozen=True)
class StageResult:
    returncode: int | None
    timed_out: bool
    elapsed_s: float
    pgid: int


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_group(pgid: int, grace_s: float = 5.0) -> bool:
    for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not group_alive(pgid):
                return True
            time.sleep(0.05)
    return not group_alive(pgid)


def run_stage(argv: list, cwd, timeout_s: float, env: dict | None,
              stdout_path, stderr_path) -> StageResult:
    t0 = time.monotonic()
    with open(stdout_path, "wb") as so, open(stderr_path, "wb") as se:
        p = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=so, stderr=se,
                             start_new_session=True)   # new session ⇒ new process group, pgid == pid
        pgid = os.getpgid(p.pid)
        try:
            rc = p.wait(timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_group(pgid)
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            rc, timed_out = None, True
    # Reap any stragglers that re-parented; verified by group_alive below.
    kill_group(pgid, grace_s=0.5)
    return StageResult(returncode=rc, timed_out=timed_out, elapsed_s=time.monotonic() - t0, pgid=pgid)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_procs -v`
Expected: 3 tests PASS (the timeout test takes ~1.5 s)

- [ ] **Step 5: Commit**

```bash
git add common/skills/agent-loop/scripts/procs.py common/skills/agent-loop/scripts/test_procs.py
git commit -m "agent-loop: process-group stage runner with verified kill"
```

---

### Task 5: Contracts — result.md parser and tasks.toml validator

**Files:**
- Create: `common/skills/agent-loop/scripts/contracts.py`
- Test: `common/skills/agent-loop/scripts/test_contracts.py`

**Interfaces:**
- Produces: `contracts.parse_result(text: str) -> Result(status: str, reason: str, base: str, files: list[str], evidence: str, unverified: str, next: str)`; raises `contracts.ProtocolError` when `STATUS` is missing or not in `{pass, fail, blocked}`. Lenient: case-insensitive keys, optional `#`/`*`/`-` prefixes, `:` or `=` separators, any order, extra lines ignored.
- Produces: `contracts.validate_tasks(data: dict) -> list[str]` (empty list = valid) and `contracts.load_tasks(path) -> list[Task]` where `Task(id: str, slug: str, summary: str, allowed_files: list[str], may_edit_tests: bool, visual: bool, timeout_s: int, verification_commands: list[str], acceptance: list[str], invariants: list[str], out_of_scope: list[str], stop_when: list[str], figma_nodes: list[str])`.
- Schema (TOML): top-level `[[tasks]]` array; required per task: `id` (`^\d{3}$`), `slug`, `summary`, `allowed_files` (non-empty), `verification_commands` (non-empty), `acceptance` (non-empty); optional with defaults: `may_edit_tests=false`, `visual=false`, `timeout_s=4500`, others `[]`. IDs must be unique and ascending.

- [ ] **Step 1: Write the failing test**

```python
# test_contracts.py
import pathlib, tempfile, unittest
import contracts

GOOD = """
# Result
STATUS: pass
REASON: none
BASE: abc123
FILES: app/a.rb, app/b.rb
EVIDENCE: ran bin/rails test test/a_test.rb → 3 runs, 0 failures
UNVERIFIED: none
NEXT: none
"""
SLOPPY = """
## Reslt (typo heading)
* status = Fail
- reason: test
files: app/a.rb
some chatter the model added
Next: fix the assertion in a_test.rb
"""
NO_STATUS = "REASON: none\nFILES: x\n"

class ParseResultTests(unittest.TestCase):
    def test_parses_well_formed(self):
        r = contracts.parse_result(GOOD)
        self.assertEqual((r.status, r.reason, r.base), ("pass", "none", "abc123"))
        self.assertEqual(r.files, ["app/a.rb", "app/b.rb"])
        self.assertIn("0 failures", r.evidence)
    def test_lenient_on_format(self):
        r = contracts.parse_result(SLOPPY)
        self.assertEqual(r.status, "fail"); self.assertEqual(r.reason, "test")
        self.assertEqual(r.files, ["app/a.rb"]); self.assertIn("assertion", r.next)
    def test_missing_status_is_protocol_error(self):
        with self.assertRaises(contracts.ProtocolError):
            contracts.parse_result(NO_STATUS)
    def test_bad_status_value_is_protocol_error(self):
        with self.assertRaises(contracts.ProtocolError):
            contracts.parse_result("STATUS: maybe\n")
    def test_missing_optional_fields_default_empty(self):
        r = contracts.parse_result("STATUS: blocked\nREASON: owner\n")
        self.assertEqual(r.files, []); self.assertEqual(r.base, "")

TASKS = """
[[tasks]]
id = "001"
slug = "well-component-skeleton"
summary = "Add ZUI::Well component class with typed slots"
allowed_files = ["app/views/components/zui/well/**"]
verification_commands = ["bin/rails test test/components/zui/well_test.rb"]
acceptance = ["AC-1: renders header/body/footer slots"]
may_edit_tests = true
visual = false
[[tasks]]
id = "002"
slug = "well-scss-sidecar"
summary = "Move Cable 2 .well styles into the sidecar"
allowed_files = ["app/views/components/zui/well/well.scss", "app/assets/stylesheets/cable_2/manifest.scss"]
verification_commands = ["bin/lint-scss app/views/components/zui/well"]
acceptance = ["AC-2: legacy source removed, tombstone present"]
visual = true
figma_nodes = ["9281-1211"]
"""

class TasksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.p = pathlib.Path(self.tmp.name) / "tasks.toml"
    def tearDown(self):
        self.tmp.cleanup()
    def test_loads_valid_manifest_with_defaults(self):
        self.p.write_text(TASKS)
        ts = contracts.load_tasks(self.p)
        self.assertEqual([t.id for t in ts], ["001", "002"])
        self.assertTrue(ts[0].may_edit_tests); self.assertFalse(ts[1].may_edit_tests)
        self.assertEqual(ts[1].timeout_s, 4500); self.assertEqual(ts[1].figma_nodes, ["9281-1211"])
    def test_validation_errors_are_specific(self):
        bad = {"tasks": [
            {"id": "1", "slug": "x", "summary": "s", "allowed_files": [], "verification_commands": ["c"], "acceptance": ["a"]},
            {"id": "001", "slug": "y", "summary": "s", "allowed_files": ["f"], "verification_commands": [], "acceptance": ["a"]},
            {"id": "001", "slug": "z", "summary": "s", "allowed_files": ["f"], "verification_commands": ["c"]},
        ]}
        errs = contracts.validate_tasks(bad)
        joined = "\n".join(errs)
        self.assertIn("tasks[0].id", joined)              # bad format
        self.assertIn("tasks[0].allowed_files", joined)   # empty
        self.assertIn("tasks[1].verification_commands", joined)
        self.assertIn("tasks[2].acceptance", joined)      # missing
        self.assertIn("duplicate id 001", joined)
    def test_empty_manifest_is_invalid(self):
        self.assertTrue(contracts.validate_tasks({}))
        self.assertTrue(contracts.validate_tasks({"tasks": []}))
    def test_load_raises_on_invalid(self):
        self.p.write_text("[[tasks]]\nid='001'\n")
        with self.assertRaises(contracts.ManifestError):
            contracts.load_tasks(self.p)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_contracts -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `contracts.py`**

```python
"""Worker result.md parsing (lenient format, strict meaning) and tasks.toml validation."""
from __future__ import annotations
import dataclasses
import pathlib
import re
import tomllib

STATUSES = {"pass", "fail", "blocked"}
REASONS = {"none", "implementation", "test", "environment", "timeout", "owner", "protocol"}
_KEY_RE = re.compile(r"^\s*(?:[#*\-]+\s*)?(status|reason|base|files|evidence|unverified|next)\s*[:=]\s*(.*)$", re.I)
_ID_RE = re.compile(r"^\d{3}$")


class ProtocolError(ValueError):
    pass


class ManifestError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Result:
    status: str
    reason: str = ""
    base: str = ""
    files: list = dataclasses.field(default_factory=list)
    evidence: str = ""
    unverified: str = ""
    next: str = ""


def parse_result(text: str) -> Result:
    found = {}
    for line in text.splitlines():
        m = _KEY_RE.match(line)
        if m:
            found.setdefault(m.group(1).lower(), m.group(2).strip())
    status = found.get("status", "").lower()
    if status not in STATUSES:
        raise ProtocolError(f"result.md missing or invalid STATUS (got {found.get('status')!r})")
    reason = found.get("reason", "").lower()
    files = [f.strip() for f in re.split(r"[,\s]+", found.get("files", "")) if f.strip()]
    return Result(status=status, reason=reason, base=found.get("base", ""), files=files,
                  evidence=found.get("evidence", ""), unverified=found.get("unverified", ""),
                  next=found.get("next", ""))


@dataclasses.dataclass(frozen=True)
class Task:
    id: str
    slug: str
    summary: str
    allowed_files: list
    verification_commands: list
    acceptance: list
    may_edit_tests: bool = False
    visual: bool = False
    timeout_s: int = 4500
    invariants: list = dataclasses.field(default_factory=list)
    out_of_scope: list = dataclasses.field(default_factory=list)
    stop_when: list = dataclasses.field(default_factory=list)
    figma_nodes: list = dataclasses.field(default_factory=list)


_REQ_STR = ("id", "slug", "summary")
_REQ_LIST = ("allowed_files", "verification_commands", "acceptance")


def validate_tasks(data: dict) -> list:
    errs = []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not tasks:
        return ["tasks: manifest must contain at least one [[tasks]] entry"]
    seen = set()
    prev = ""
    for i, t in enumerate(tasks):
        p = f"tasks[{i}]"
        for k in _REQ_STR:
            if not isinstance(t.get(k), str) or not t.get(k).strip():
                errs.append(f"{p}.{k}: required non-empty string")
        for k in _REQ_LIST:
            v = t.get(k)
            if not isinstance(v, list) or not v:
                errs.append(f"{p}.{k}: required non-empty list")
        tid = str(t.get("id", ""))
        if tid and not _ID_RE.match(tid):
            errs.append(f"{p}.id: must match ^\\d{{3}}$ (got {tid!r})")
        if tid in seen:
            errs.append(f"{p}.id: duplicate id {tid}")
        seen.add(tid)
        if tid and prev and tid <= prev:
            errs.append(f"{p}.id: ids must be ascending ({prev} then {tid})")
        prev = tid or prev
        for k in ("may_edit_tests", "visual"):
            if k in t and not isinstance(t[k], bool):
                errs.append(f"{p}.{k}: must be boolean")
        if "timeout_s" in t and (not isinstance(t["timeout_s"], int) or t["timeout_s"] <= 0):
            errs.append(f"{p}.timeout_s: must be positive int")
    return errs


def load_tasks(path) -> list:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    errs = validate_tasks(data)
    if errs:
        raise ManifestError(f"{path}:\n  " + "\n  ".join(errs))
    fields = {f.name for f in dataclasses.fields(Task)}
    return [Task(**{k: v for k, v in t.items() if k in fields}) for t in data["tasks"]]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_contracts -v`
Expected: 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add common/skills/agent-loop/scripts/contracts.py common/skills/agent-loop/scripts/test_contracts.py
git commit -m "agent-loop: result.md parser (lenient/strict) and tasks.toml validator"
```

---

### Task 6: Worktree snapshots and the diff allowlist

**Files:**
- Create: `common/skills/agent-loop/scripts/worktree.py`
- Test: `common/skills/agent-loop/scripts/test_worktree.py`

**Interfaces:**
- Consumes: `config.Config.protected_paths`, `config.Config.test_path_globs`, `contracts.Task`.
- Produces: `worktree.snapshot(wt: Path) -> str` (tree OID of the full working state incl. untracked, via a temp index: `git add -A` into `GIT_INDEX_FILE=<tmp>` then `git write-tree`); `worktree.restore(wt, tree_oid) -> None` (`git read-tree --reset -u <oid>` then `git clean -fd` limited to paths not ignored); `worktree.changed_paths(wt, base_tree_oid) -> list[str]` (`git diff --name-only <base> <snapshot(now)>`); `worktree.check_allowlist(paths, task, protected_paths, test_globs) -> list[str]` returning human-readable violations (empty = OK); `worktree.head(wt) -> str`.
- Glob semantics: `fnmatch`-style with `**` meaning any depth; a pattern ending in `/` matches the directory prefix.

- [ ] **Step 1: Write the failing test**

```python
# test_worktree.py
import os, pathlib, subprocess, tempfile, unittest
import worktree
from contracts import Task

def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, check=True).stdout

class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.wt = pathlib.Path(self.tmp.name)
        git(self.wt, "init", "-q", "-b", "main")
        git(self.wt, "config", "user.email", "t@t"); git(self.wt, "config", "user.name", "t")
        (self.wt/"app").mkdir(); (self.wt/"app"/"a.rb").write_text("a\n")
        (self.wt/".gitignore").write_text("ignored.txt\n")
        git(self.wt, "add", "-A"); git(self.wt, "commit", "-qm", "init")
    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_includes_untracked_and_restore_reverts(self):
        base = worktree.snapshot(self.wt)
        (self.wt/"app"/"a.rb").write_text("changed\n")
        (self.wt/"app"/"new.rb").write_text("new\n")
        (self.wt/"ignored.txt").write_text("keep me\n")
        after = worktree.snapshot(self.wt)
        self.assertNotEqual(base, after)
        self.assertEqual(sorted(worktree.changed_paths(self.wt, base)), ["app/a.rb", "app/new.rb"])
        worktree.restore(self.wt, base)
        self.assertEqual((self.wt/"app"/"a.rb").read_text(), "a\n")
        self.assertFalse((self.wt/"app"/"new.rb").exists())
        self.assertTrue((self.wt/"ignored.txt").exists())   # ignored files survive restore
        self.assertEqual(git(self.wt, "status", "--porcelain").strip(), "")

    def test_snapshot_does_not_touch_real_index(self):
        (self.wt/"app"/"new.rb").write_text("new\n")
        worktree.snapshot(self.wt)
        self.assertIn("?? app/new.rb", git(self.wt, "status", "--porcelain"))

    def test_head(self):
        self.assertEqual(worktree.head(self.wt), git(self.wt, "rev-parse", "HEAD").strip())

class AllowlistTests(unittest.TestCase):
    PROT = ["bin/", ".github/", "AGENTS.md"]
    TESTS = ["test/**", "**/fixtures/**"]
    def task(self, allowed, may_edit_tests=False):
        return Task(id="001", slug="s", summary="s", allowed_files=allowed, verification_commands=["c"], acceptance=["a"], may_edit_tests=may_edit_tests)

    def test_all_within_allowlist_ok(self):
        v = worktree.check_allowlist(["app/views/components/zui/well/well.rb"], self.task(["app/views/components/zui/well/**"]), self.PROT, self.TESTS)
        self.assertEqual(v, [])
    def test_outside_allowlist_flagged(self):
        v = worktree.check_allowlist(["app/models/user.rb"], self.task(["app/views/**"]), self.PROT, self.TESTS)
        self.assertEqual(len(v), 1); self.assertIn("app/models/user.rb", v[0]); self.assertIn("not in allowed_files", v[0])
    def test_protected_always_flagged_even_if_allowed(self):
        v = worktree.check_allowlist(["bin/rails", "AGENTS.md"], self.task(["**"]), self.PROT, self.TESTS)
        self.assertEqual(len(v), 2); self.assertTrue(all("protected" in x for x in v))
    def test_tests_flagged_without_permission(self):
        v = worktree.check_allowlist(["test/components/well_test.rb"], self.task(["**"]), self.PROT, self.TESTS)
        self.assertEqual(len(v), 1); self.assertIn("may_edit_tests", v[0])
    def test_tests_ok_with_permission(self):
        v = worktree.check_allowlist(["test/components/well_test.rb", "spec/x/fixtures/f.yml"], self.task(["**"], may_edit_tests=True), self.PROT, self.TESTS)
        self.assertEqual(v, [])
    def test_dir_prefix_pattern(self):
        self.assertEqual(worktree.check_allowlist(["app/x/y/z.rb"], self.task(["app/x/"]), self.PROT, self.TESTS), [])

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_worktree -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `worktree.py`**

```python
"""Git snapshots of the whole working state (tracked + untracked, not ignored), restore,
changed-path listing, and the runner-enforced diff allowlist."""
from __future__ import annotations
import fnmatch
import os
import pathlib
import re
import subprocess
import tempfile


def _git(wt, *args, env=None) -> str:
    r = subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def head(wt) -> str:
    return _git(wt, "rev-parse", "HEAD").strip()


def snapshot(wt) -> str:
    """Tree OID of the current working state using a throwaway index (real index untouched)."""
    with tempfile.NamedTemporaryFile(prefix="al-index-", delete=False) as tf:
        idx = tf.name
    try:
        os.unlink(idx)
        env = {**os.environ, "GIT_INDEX_FILE": idx}
        _git(wt, "add", "-A", env=env)
        return _git(wt, "write-tree", env=env).strip()
    finally:
        try:
            os.unlink(idx)
        except FileNotFoundError:
            pass


def changed_paths(wt, base_tree: str) -> list:
    now = snapshot(wt)
    out = _git(wt, "diff", "--name-only", base_tree, now)
    return [l.strip() for l in out.splitlines() if l.strip()]


def restore(wt, tree: str) -> None:
    """Make the working tree match `tree` exactly for tracked-or-added content; ignored files survive."""
    _git(wt, "read-tree", "--reset", "-u", tree)
    _git(wt, "clean", "-fd")            # remove untracked (non-ignored) leftovers
    # read-tree left the real index pointing at `tree`; put it back to HEAD so status is sane.
    _git(wt, "reset", "-q")


def _match(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern) or path == pattern.rstrip("/")
    if "**" in pattern:
        rx = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"/\*\*", "(?:/.*)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        return re.fullmatch(rx, path) is not None
    return fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern.rstrip("*"))


def check_allowlist(paths: list, task, protected_paths: list, test_globs: list) -> list:
    violations = []
    for p in paths:
        if any(_match(p, pp) for pp in protected_paths):
            violations.append(f"{p}: protected path (never editable by workers)")
            continue
        if not any(_match(p, a) for a in task.allowed_files):
            violations.append(f"{p}: not in allowed_files for task {task.id}")
            continue
        if not task.may_edit_tests and any(_match(p, g) for g in test_globs):
            violations.append(f"{p}: test/fixture path but task {task.id} has may_edit_tests=false")
    return violations
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_worktree -v`
Expected: 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add common/skills/agent-loop/scripts/worktree.py common/skills/agent-loop/scripts/test_worktree.py
git commit -m "agent-loop: worktree snapshot/restore and runner-enforced diff allowlist"
```

---

### Task 7: Agent definition loader → `pi` argv

**Files:**
- Create: `common/skills/agent-loop/scripts/agentdef.py`
- Test: `common/skills/agent-loop/scripts/test_agentdef.py`

**Interfaces:**
- Consumes: rendered agent files at `<pi_agents_dir>/<name>.md` (YAML-ish frontmatter: `name`, `description`, `model`, `thinking`, `tools`, optionally `fallbackModels`).
- Produces: `agentdef.load(pi_agents_dir: Path, name: str) -> AgentDef(name, model: str, thinking: str, tools: list[str], body: str, fallback_models: list[str])`; `agentdef.assert_worker_safe(a: AgentDef) -> None` (raises `AgentDefError` if `fallback_models` non-empty or `tools` contains anything outside `{read, grep, find, ls, bash, edit, write}`); `agentdef.pi_argv(a: AgentDef, prompt_file: Path, session_dir: Path, body_file: Path) -> list[str]` producing `["pi", "-p", "--model", f"{model}:{thinking}", "--tools", ",".join(tools), "--append-system-prompt", str(body_file), "--session-dir", str(session_dir), "--no-prompt-templates", "@<prompt_file>"]`. (The prompt is passed as a file reference so long task manifests never hit argv limits; `pi` reads `@path` as file contents.)
- Frontmatter parser is deliberately minimal: `key: value` lines between the first two `---` lines; list values are comma-separated.

- [ ] **Step 1: Write the failing test**

```python
# test_agentdef.py
import pathlib, tempfile, unittest
import agentdef

WORKER = """---
name: cloud-worker
description: Cheap cloud worker.
model: ollama-cloud/deepseek-v4-flash
thinking: medium
tools: read, grep, find, ls, bash, edit, write
---

You are a worker. Body line two.
"""
UNSAFE_FALLBACK = WORKER.replace("tools:", "fallbackModels: anthropic/claude-sonnet-5\ntools:")
UNSAFE_TOOLS = WORKER.replace("edit, write", "edit, write, web_search")

class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = pathlib.Path(self.tmp.name)
        (self.d/"cloud-worker.md").write_text(WORKER)
    def tearDown(self):
        self.tmp.cleanup()

    def test_load_parses_frontmatter_and_body(self):
        a = agentdef.load(self.d, "cloud-worker")
        self.assertEqual(a.model, "ollama-cloud/deepseek-v4-flash"); self.assertEqual(a.thinking, "medium")
        self.assertEqual(a.tools, ["read", "grep", "find", "ls", "bash", "edit", "write"])
        self.assertTrue(a.body.startswith("You are a worker.")); self.assertEqual(a.fallback_models, [])
    def test_missing_agent_raises(self):
        with self.assertRaises(agentdef.AgentDefError):
            agentdef.load(self.d, "nope")
    def test_worker_safe_ok(self):
        agentdef.assert_worker_safe(agentdef.load(self.d, "cloud-worker"))
    def test_worker_with_fallback_rejected(self):
        (self.d/"bad.md").write_text(UNSAFE_FALLBACK)
        with self.assertRaisesRegex(agentdef.AgentDefError, "fallbackModels"):
            agentdef.assert_worker_safe(agentdef.load(self.d, "bad"))
    def test_worker_with_extra_tool_rejected(self):
        (self.d/"bad.md").write_text(UNSAFE_TOOLS)
        with self.assertRaisesRegex(agentdef.AgentDefError, "web_search"):
            agentdef.assert_worker_safe(agentdef.load(self.d, "bad"))
    def test_pi_argv_shape(self):
        a = agentdef.load(self.d, "cloud-worker")
        argv = agentdef.pi_argv(a, self.d/"prompt.md", self.d/"sess", self.d/"body.md")
        self.assertEqual(argv[:2], ["pi", "-p"])
        self.assertIn("--model", argv); self.assertEqual(argv[argv.index("--model")+1], "ollama-cloud/deepseek-v4-flash:medium")
        self.assertEqual(argv[argv.index("--tools")+1], "read,grep,find,ls,bash,edit,write")
        self.assertEqual(argv[argv.index("--append-system-prompt")+1], str(self.d/"body.md"))
        self.assertEqual(argv[argv.index("--session-dir")+1], str(self.d/"sess"))
        self.assertEqual(argv[-1], f"@{self.d/'prompt.md'}")

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_agentdef -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `agentdef.py`**

```python
"""Read a rendered pi agent definition and turn it into a `pi -p` command line."""
from __future__ import annotations
import dataclasses
import pathlib

WORKER_TOOLS = {"read", "grep", "find", "ls", "bash", "edit", "write"}


class AgentDefError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class AgentDef:
    name: str
    model: str
    thinking: str
    tools: list
    body: str
    fallback_models: list


def _split_frontmatter(text: str):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AgentDefError("agent file has no frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise AgentDefError("unterminated frontmatter")
    fm = {}
    for l in lines[1:end]:
        if ":" in l:
            k, v = l.split(":", 1)
            fm[k.strip()] = v.strip()
    body = "\n".join(lines[end + 1:]).strip() + "\n"
    return fm, body


def _list(v: str) -> list:
    return [x.strip() for x in v.split(",") if x.strip()] if v else []


def load(pi_agents_dir, name: str) -> AgentDef:
    p = pathlib.Path(pi_agents_dir) / f"{name}.md"
    if not p.exists():
        raise AgentDefError(f"no agent definition at {p}")
    fm, body = _split_frontmatter(p.read_text())
    for k in ("model", "thinking"):
        if k not in fm:
            raise AgentDefError(f"{p}: frontmatter missing {k}")
    return AgentDef(name=fm.get("name", name), model=fm["model"], thinking=fm["thinking"],
                    tools=_list(fm.get("tools", "")), body=body,
                    fallback_models=_list(fm.get("fallbackModels", "")))


def assert_worker_safe(a: AgentDef) -> None:
    if a.fallback_models:
        raise AgentDefError(f"{a.name}: worker definitions must not declare fallbackModels (got {a.fallback_models}); the runner owns escalation")
    extra = sorted(set(a.tools) - WORKER_TOOLS)
    if extra:
        raise AgentDefError(f"{a.name}: worker tools outside allowlist: {extra}")
    if not a.tools:
        raise AgentDefError(f"{a.name}: worker must declare an explicit tools allowlist")


def pi_argv(a: AgentDef, prompt_file, session_dir, body_file) -> list:
    return ["pi", "-p",
            "--model", f"{a.model}:{a.thinking}",
            "--tools", ",".join(a.tools),
            "--append-system-prompt", str(body_file),
            "--session-dir", str(session_dir),
            "--no-prompt-templates",
            f"@{prompt_file}"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_agentdef -v`
Expected: 6 tests PASS

- [ ] **Step 5: Verify the `@file` prompt convention against the installed pi**

Run: `cd /tmp && printf 'reply with the single word pong' > al-probe.md && pi -p --no-session --tools read "@/tmp/al-probe.md" 2>&1 | tail -2`
Expected: output contains `pong`. If pi does **not** expand `@path`, change `pi_argv` to read the file and pass its contents as the final argv element, update `test_pi_argv_shape` accordingly, and note the deviation in the commit message.

- [ ] **Step 6: Commit**

```bash
git add common/skills/agent-loop/scripts/agentdef.py common/skills/agent-loop/scripts/test_agentdef.py
git commit -m "agent-loop: agent-def loader and pi argv builder; worker safety asserts"
```

---

### Task 8: Worker agent definitions and model tiers

**Files:**
- Create: `common/agents/cloud-worker.agent.md`, `common/agents/local-worker.agent.md`, `common/agents/premium-worker.agent.md`
- Modify: `common/model-tiers.toml`
- Test: `common/skills/agent-loop/scripts/test_agent_sources.py`

**Interfaces:**
- Produces: three source agents with `tier:` pointing at new tables and `access: Read, Grep, Glob, Bash, Write, Edit` (renders to pi `tools: read, grep, find, bash, write, edit` per `tools/pi/setup.md`; `ls` is added by the renderer's default set only when `access` is absent, so the runner's `WORKER_TOOLS` superset tolerates its absence).
- Model IDs in `[worker-cloud]` / `[worker-local]` are **placeholders until Task 14 verifies them**; the plan records the values to update.

- [ ] **Step 1: Write the failing test**

```python
# test_agent_sources.py — guards the source-of-truth agent files, not the rendered ones
import pathlib, re, tomllib, unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]          # agentfiles repo
AGENTS = ROOT / "common" / "agents"
TIERS = ROOT / "common" / "model-tiers.toml"
WORKERS = ("cloud-worker", "local-worker", "premium-worker")

def fm(path):
    text = path.read_text().splitlines()
    end = text.index("---", 1)
    return dict(l.split(":", 1) for l in text[1:end] if ":" in l)

class WorkerSourceTests(unittest.TestCase):
    def test_worker_sources_exist_with_tier_and_access(self):
        for w in WORKERS:
            f = fm(AGENTS / f"{w}.agent.md")
            self.assertEqual(f["name"].strip(), w)
            self.assertEqual(f["tier"].strip(), f"worker-{w.split('-')[0]}")
            self.assertEqual(f["access"].strip(), "Read, Grep, Glob, Bash, Write, Edit")
            self.assertNotIn("fallbackModels", f)
    def test_tiers_define_all_five_new_tables_with_all_keys(self):
        t = tomllib.loads(TIERS.read_text())
        for name in ("worker-cloud", "worker-local", "worker-premium", "flagship-author", "flagship-critic"):
            self.assertIn(name, t, name)
            for k in ("claude", "codex_model", "codex_effort", "pi_model", "pi_thinking"):
                self.assertIn(k, t[name], f"{name}.{k}")
        self.assertTrue(t["worker-cloud"]["pi_model"].startswith("ollama-cloud/"))
        self.assertTrue(t["worker-local"]["pi_model"].startswith("ollama-local/"))
        self.assertEqual(t["flagship-author"]["pi_model"], "anthropic/claude-fable-5-1")
        self.assertEqual(t["flagship-critic"]["pi_model"], "openai-codex/gpt-6-astra")
    def test_worker_bodies_carry_the_contract(self):
        for w in WORKERS:
            body = (AGENTS / f"{w}.agent.md").read_text()
            for phrase in ("STATUS:", "result.md", "Do not commit", "allowed_files", "Never weaken"):
                self.assertIn(phrase, body, f"{w} missing {phrase!r}")

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_agent_sources -v`
Expected: FAIL with `FileNotFoundError` for `cloud-worker.agent.md`

- [ ] **Step 3: Append the tier tables to `common/model-tiers.toml`**

```toml

# --- Agent-loop tiers (ZIP-6774 pilot). Worker model IDs are verified in slice 1
# (see docs/superpowers/plans/2026-09-10-agent-loop-1-runner-core.md, Task 14). ---
[worker-cloud]
claude = "sonnet"                    # workers are pi-only; other tools get a sane default
codex_model = "gpt-5.4-mini"
codex_effort = "medium"
pi_model = "ollama-cloud/deepseek-v4-flash"
pi_thinking = "medium"

[worker-local]
claude = "sonnet"
codex_model = "gpt-5.4-mini"
codex_effort = "medium"
pi_model = "ollama-local/gpt-oss:20b"
pi_thinking = "medium"

[worker-premium]
claude = "sonnet"
codex_model = "gpt-5.4"
codex_effort = "high"
pi_model = "openai-codex/gpt-5.6-terra"
pi_thinking = "max"

[flagship-author]
claude = "opus"
codex_model = "gpt-5.5"
codex_effort = "high"
pi_model = "anthropic/claude-fable-5-1"
pi_thinking = "high"

[flagship-critic]
claude = "opus"
codex_model = "gpt-5.5"
codex_effort = "high"
pi_model = "openai-codex/gpt-6-astra"
pi_thinking = "xhigh"
```

- [ ] **Step 4: Write `common/agents/cloud-worker.agent.md`**

```markdown
---
name: cloud-worker
description: Cheap cloud implementation worker for the autonomous agent loop. Executes ONE bounded task from a runner-supplied manifest. Never makes design decisions. Not for interactive use.
tier: worker-cloud
access: Read, Grep, Glob, Bash, Write, Edit
---

You are an implementation worker inside an automated loop. A deterministic runner
gave you exactly one task. Your only job is to complete that task within its stated
boundaries and report honestly.

## The contract

1. Read the task file you were given in full. It names the user-visible outcome,
   `allowed_files`, whether you may edit tests (`may_edit_tests`), acceptance
   criteria, verification commands, invariants, out-of-scope items, and stop
   conditions. Treat it as a verbatim contract. Do not improvise.
2. Read the repository's own instructions (`AGENTS.md`, `CLAUDE.md`) and follow
   their conventions. Where the task and the repo conventions conflict, stop and
   report `STATUS: blocked` / `REASON: owner`.
3. If a `feedback.md` exists beside the task file, read it first. It is what the
   gates saw on the previous attempt. Address it directly.
4. Edit only paths matching `allowed_files`. If the correct fix requires touching
   anything else, stop and report `STATUS: blocked` / `REASON: owner` naming the
   path. The runner rejects out-of-allowlist diffs automatically; do not try.
5. Never edit tests or fixtures unless `may_edit_tests = true`. **Never weaken** an
   acceptance check, assertion, fixture, or lint rule to make your change pass.
6. Run every command in `verification_commands` before reporting. Paste the real
   output summary into `EVIDENCE`. If a command cannot run (missing service,
   database, port), report `REASON: environment` — do not guess at success.
7. **Do not commit, push, rebase, stash, or create branches.** Leave the working
   tree exactly as your finished work. The runner snapshots and checkpoints.
8. If you discover the task is ambiguous, contradicts the codebase, or requires a
   design choice (public API shape, naming visible to callers, behavior users
   depend on), stop and report `STATUS: blocked` / `REASON: owner` with the
   question in `NEXT`. Design decisions belong to the planner, not to you.
9. Stop when the stop conditions are met or when you have nothing verifiable left
   to do. Do not pad.

## The result file

Write `result.md` in the task directory (the path is given in the task file) even
when you fail. Format, one field per line, in this order:

```
STATUS: pass | fail | blocked
REASON: none | implementation | test | environment | timeout | owner | protocol
BASE: <git rev-parse HEAD at start>
FILES: <comma-separated paths you changed>
EVIDENCE: <commands run and their result summaries>
UNVERIFIED: <anything the acceptance criteria ask for that you could not verify>
NEXT: <one concrete next step, or a question for the planner>
```

`STATUS: pass` is a claim the runner verifies independently. Overstating it wastes
a premium attempt and is worse than an honest `fail`.
```

- [ ] **Step 5: Write `common/agents/local-worker.agent.md` and `premium-worker.agent.md`**

Both files are **byte-identical to `cloud-worker.agent.md` below the frontmatter.** Frontmatter differs only in `name`, `description`, and `tier`:

```markdown
---
name: local-worker
description: Local Ollama implementation worker for the autonomous agent loop. Same contract as cloud-worker; runs only when the heavy lane is idle. Not for interactive use.
tier: worker-local
access: Read, Grep, Glob, Bash, Write, Edit
---
```

```markdown
---
name: premium-worker
description: Premium fallback implementation worker for the autonomous agent loop. One attempt per task after cheap attempts fail. Same contract as cloud-worker. Not for interactive use.
tier: worker-premium
access: Read, Grep, Glob, Bash, Write, Edit
---
```

Generate them from the cloud file so the bodies cannot drift:

```bash
cd ~/workspace/agentfiles/common/agents
body() { awk 'f{print} /^---$/{c++; if(c==2) f=1}' cloud-worker.agent.md; }
{ printf -- '---\nname: local-worker\ndescription: Local Ollama implementation worker for the autonomous agent loop. Same contract as cloud-worker; runs only when the heavy lane is idle. Not for interactive use.\ntier: worker-local\naccess: Read, Grep, Glob, Bash, Write, Edit\n---\n'; body; } > local-worker.agent.md
{ printf -- '---\nname: premium-worker\ndescription: Premium fallback implementation worker for the autonomous agent loop. One attempt per task after cheap attempts fail. Same contract as cloud-worker. Not for interactive use.\ntier: worker-premium\naccess: Read, Grep, Glob, Bash, Write, Edit\n---\n'; body; } > premium-worker.agent.md
diff <(awk 'f{print} /^---$/{c++; if(c==2) f=1}' cloud-worker.agent.md) <(awk 'f{print} /^---$/{c++; if(c==2) f=1}' local-worker.agent.md) && echo bodies-identical
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd ~/workspace/agentfiles/common/skills/agent-loop/scripts && python3 -m unittest test_agent_sources -v`
Expected: 3 tests PASS

- [ ] **Step 7: Run the repo's own scanner on the new sources**

Run: `cd ~/workspace/agentfiles && common/scripts/agentfiles-scan.sh common/agents && common/scripts/agentfiles-scan.sh common/skills/agent-loop`
Expected: no findings (these files contain no hosts, tokens, or customer text)

- [ ] **Step 8: Commit**

```bash
git add common/model-tiers.toml common/agents/cloud-worker.agent.md common/agents/local-worker.agent.md common/agents/premium-worker.agent.md common/skills/agent-loop/scripts/test_agent_sources.py
git commit -m "agent-loop: worker agent sources (no fallback, tool allowlist) and tier tables"
```

---

### Task 9: Ladder B — arm assignment, rung sequencing, feedback

**Files:**
- Create: `common/skills/agent-loop/scripts/ladder.py`
- Test: `common/skills/agent-loop/scripts/test_ladder.py`

**Interfaces:**
- Consumes: `config.Config.arms_alternate`, `config.TicketSpec.pin_arm`, `contracts.Task`.
- Produces: `ladder.assign_arm(task_index: int, visual: bool, pin: str | None, alternate: list[str]) -> str` — pinned wins; else alternates by *index within the visual stratum*: the runner passes `task_index` as the ordinal of this task among tasks with the same `visual` flag. `ladder.next_rung(attempts: list[Attempt], arm: str) -> Rung | None` where `Attempt(rung: Rung, outcome: str)` and `Rung(agent: str, tier: str, n: int)`; sequence: `(arm-worker, "cheap", 1)`, `(arm-worker, "cheap", 2)`, `("premium-worker", "premium", 1)`, then `None` (= blocked). `ladder.append_feedback(task_dir: Path, attempt_n: int, gate_summary: str) -> Path` writes/appends `feedback.md` as `## Attempt N\n<two-sentence summary>\n`.
- Only outcomes in `{"rejected", "timeout", "protocol", "unusable"}` advance the ladder; `"accepted"` ends it (`next_rung` returns `None` with `attempts[-1].outcome == "accepted"`); `"environment"` does **not** consume a rung (the runner retries the same rung after a gate-only retry, per spec).

- [ ] **Step 1: Write the failing test**

```python
# test_ladder.py
import pathlib, tempfile, unittest
import ladder
from ladder import Attempt, Rung

ALT = ["cloud", "local"]

class AssignArmTests(unittest.TestCase):
    def test_pin_wins(self):
        self.assertEqual(ladder.assign_arm(0, False, "local", ALT), "local")
        self.assertEqual(ladder.assign_arm(1, True, "cloud", ALT), "cloud")
    def test_alternates_by_index(self):
        self.assertEqual([ladder.assign_arm(i, False, None, ALT) for i in range(4)], ["cloud", "local", "cloud", "local"])
    def test_invalid_pin_raises(self):
        with self.assertRaises(ValueError):
            ladder.assign_arm(0, False, "premium", ALT)

class NextRungTests(unittest.TestCase):
    def test_sequence_for_cloud_arm(self):
        r1 = ladder.next_rung([], "cloud")
        self.assertEqual(r1, Rung("cloud-worker", "cheap", 1))
        r2 = ladder.next_rung([Attempt(r1, "rejected")], "cloud")
        self.assertEqual(r2, Rung("cloud-worker", "cheap", 2))
        r3 = ladder.next_rung([Attempt(r1, "rejected"), Attempt(r2, "timeout")], "cloud")
        self.assertEqual(r3, Rung("premium-worker", "premium", 1))
        self.assertIsNone(ladder.next_rung([Attempt(r1, "rejected"), Attempt(r2, "timeout"), Attempt(r3, "protocol")], "cloud"))
    def test_local_arm_uses_local_worker(self):
        self.assertEqual(ladder.next_rung([], "local").agent, "local-worker")
    def test_accepted_ends_ladder(self):
        r1 = ladder.next_rung([], "cloud")
        self.assertIsNone(ladder.next_rung([Attempt(r1, "accepted")], "cloud"))
    def test_environment_does_not_consume_rung(self):
        r1 = ladder.next_rung([], "cloud")
        self.assertEqual(ladder.next_rung([Attempt(r1, "environment")], "cloud"), r1)
        self.assertEqual(ladder.next_rung([Attempt(r1, "environment"), Attempt(r1, "environment")], "cloud"), r1)

class FeedbackTests(unittest.TestCase):
    def test_append_feedback_accumulates(self):
        with tempfile.TemporaryDirectory() as d:
            p = ladder.append_feedback(pathlib.Path(d), 1, "Lint failed: trailing whitespace in well.rb:12. Tests were not run.")
            ladder.append_feedback(pathlib.Path(d), 2, "Test well_test.rb:33 expects a footer slot; none rendered.")
            text = p.read_text()
            self.assertIn("## Attempt 1", text); self.assertIn("## Attempt 2", text); self.assertIn("footer slot", text)
            self.assertEqual(p.name, "feedback.md")

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_ladder -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `ladder.py`**

```python
"""Escalation ladder B: cheap ×2 (with gate feedback) → premium ×1 → blocked."""
from __future__ import annotations
import dataclasses
import pathlib

CHEAP_ARMS = ("cloud", "local")
ADVANCING = {"rejected", "timeout", "protocol", "unusable"}


@dataclasses.dataclass(frozen=True)
class Rung:
    agent: str
    tier: str      # "cheap" | "premium"
    n: int


@dataclasses.dataclass(frozen=True)
class Attempt:
    rung: Rung
    outcome: str   # accepted | rejected | timeout | protocol | unusable | environment


def assign_arm(task_index: int, visual: bool, pin: str | None, alternate: list) -> str:
    if pin is not None:
        if pin not in CHEAP_ARMS:
            raise ValueError(f"pin_arm must be one of {CHEAP_ARMS}, got {pin!r}")
        return pin
    return alternate[task_index % len(alternate)]


def _sequence(arm: str) -> list:
    return [Rung(f"{arm}-worker", "cheap", 1), Rung(f"{arm}-worker", "cheap", 2), Rung("premium-worker", "premium", 1)]


def next_rung(attempts: list, arm: str) -> Rung | None:
    if attempts and attempts[-1].outcome == "accepted":
        return None
    consumed = sum(1 for a in attempts if a.outcome in ADVANCING)
    seq = _sequence(arm)
    return seq[consumed] if consumed < len(seq) else None


def append_feedback(task_dir, attempt_n: int, gate_summary: str) -> pathlib.Path:
    p = pathlib.Path(task_dir) / "feedback.md"
    with open(p, "a") as f:
        f.write(f"## Attempt {attempt_n}\n{gate_summary.strip()}\n\n")
    return p
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_ladder -v`
Expected: 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add common/skills/agent-loop/scripts/ladder.py common/skills/agent-loop/scripts/test_ladder.py
git commit -m "agent-loop: ladder B sequencing, arm assignment, feedback file"
```

---

### Task 10: Ticket state machine and operator files

**Files:**
- Create: `common/skills/agent-loop/scripts/state.py`
- Test: `common/skills/agent-loop/scripts/test_state.py`

**Interfaces:**
- Produces: `state.STATES` (ordered tuple): `queued, spinup, plan, plan-review, implement, gates, draft-pr, ready, bot-loop, human-gate-1, colleague-loop, human-gate-2, done, blocked, paused`; `state.EDGES: dict[str, set[str]]` (forward edges plus `* → blocked|paused`, `paused → previous`, `blocked → implement|plan`, `bot-loop|colleague-loop|ready → gates` for the new-SHA rule); `state.Ticket(key, state, previous, worktree, branch, workspace, head_sha, evidence_sha, author_vendor, critic_vendor, attempts: dict[task_id, list[dict]], decisions: list[dict], reason, updated_utc)`; `state.load(ticket_dir) -> Ticket` (creates `queued` if absent); `state.save(ticket_dir, t)` (atomic write via temp+rename); `state.transition(t, to: str, reason: str = "") -> Ticket` raising `state.IllegalTransition`; `state.paused(state_root) -> bool` (`PAUSE` file), `state.human_owned(ticket_dir) -> bool` (`HUMAN` file); `state.assign_vendors(index: int) -> tuple[str, str]` (odd index → `("fable", "astra")`, even → `("astra", "fable")` — spec's deterministic alternation).

- [ ] **Step 1: Write the failing test**

```python
# test_state.py
import json, pathlib, tempfile, unittest
import state

class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = pathlib.Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_new_ticket_is_queued_and_persists_atomically(self):
        t = state.load(self.d / "ZIP-1")
        self.assertEqual(t.state, "queued"); self.assertEqual(t.key, "ZIP-1")
        state.save(self.d / "ZIP-1", t)
        self.assertTrue((self.d / "ZIP-1" / "state.json").exists())
        self.assertFalse(list((self.d / "ZIP-1").glob("*.tmp")))
        self.assertEqual(state.load(self.d / "ZIP-1").state, "queued")

    def test_forward_path(self):
        t = state.load(self.d / "Z")
        for s in ("spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready", "bot-loop", "human-gate-1", "colleague-loop", "human-gate-2", "done"):
            t = state.transition(t, s)
        self.assertEqual(t.state, "done")

    def test_illegal_skip_raises(self):
        t = state.load(self.d / "Z")
        with self.assertRaises(state.IllegalTransition):
            state.transition(t, "ready")

    def test_new_sha_rule_returns_to_gates(self):
        t = state.load(self.d / "Z")
        for s in ("spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready", "bot-loop"):
            t = state.transition(t, s)
        t = state.transition(t, "gates", reason="new commit abc")
        self.assertEqual(t.state, "gates"); self.assertEqual(t.previous, "bot-loop")

    def test_pause_and_resume(self):
        t = state.transition(state.load(self.d / "Z"), "spinup")
        t = state.transition(t, "paused", reason="resource")
        self.assertEqual(t.reason, "resource")
        t = state.transition(t, "spinup")           # resume to previous only
        self.assertEqual(t.state, "spinup")
        with self.assertRaises(state.IllegalTransition):
            state.transition(state.transition(t, "paused"), "gates")

    def test_blocked_can_only_return_to_implement_or_plan(self):
        t = state.load(self.d / "Z")
        for s in ("spinup", "plan", "plan-review", "implement"):
            t = state.transition(t, s)
        t = state.transition(t, "blocked", reason="ladder exhausted")
        self.assertEqual(state.transition(t, "plan").state, "plan")
        with self.assertRaises(state.IllegalTransition):
            state.transition(t, "gates")

    def test_operator_files(self):
        root = self.d
        self.assertFalse(state.paused(root)); (root / "PAUSE").touch(); self.assertTrue(state.paused(root))
        td = root / "tickets" / "Z"; td.mkdir(parents=True)
        self.assertFalse(state.human_owned(td)); (td / "HUMAN").touch(); self.assertTrue(state.human_owned(td))

    def test_vendor_alternation(self):
        self.assertEqual(state.assign_vendors(1), ("fable", "astra"))
        self.assertEqual(state.assign_vendors(2), ("astra", "fable"))

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_state -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `state.py`**

```python
"""Per-ticket lifecycle state, persisted as state.json with atomic writes."""
from __future__ import annotations
import dataclasses
import datetime as dt
import json
import os
import pathlib

STATES = ("queued", "spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready",
          "bot-loop", "human-gate-1", "colleague-loop", "human-gate-2", "done", "blocked", "paused")
_FORWARD = ("queued", "spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready",
            "bot-loop", "human-gate-1", "colleague-loop", "human-gate-2", "done")

EDGES = {s: set() for s in STATES}
for a, b in zip(_FORWARD, _FORWARD[1:]):
    EDGES[a].add(b)
for s in STATES:
    if s not in ("done", "blocked", "paused"):
        EDGES[s] |= {"blocked", "paused"}
EDGES["blocked"] |= {"implement", "plan"}
for s in ("ready", "bot-loop", "colleague-loop", "human-gate-1", "human-gate-2"):
    EDGES[s].add("gates")            # any new SHA invalidates evidence
EDGES["plan-review"].add("plan")     # critic sends the plan back
EDGES["paused"] = set()              # resolved dynamically: only `previous`


class IllegalTransition(ValueError):
    pass


@dataclasses.dataclass
class Ticket:
    key: str
    state: str = "queued"
    previous: str = ""
    worktree: str = ""
    branch: str = ""
    workspace: str = ""
    head_sha: str = ""
    evidence_sha: str = ""
    author_vendor: str = ""
    critic_vendor: str = ""
    attempts: dict = dataclasses.field(default_factory=dict)
    decisions: list = dataclasses.field(default_factory=list)
    reason: str = ""
    updated_utc: str = ""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(ticket_dir) -> Ticket:
    ticket_dir = pathlib.Path(ticket_dir)
    p = ticket_dir / "state.json"
    if not p.exists():
        return Ticket(key=ticket_dir.name, updated_utc=_now())
    return Ticket(**json.loads(p.read_text()))


def save(ticket_dir, t: Ticket) -> None:
    ticket_dir = pathlib.Path(ticket_dir)
    ticket_dir.mkdir(parents=True, exist_ok=True)
    t.updated_utc = _now()
    tmp = ticket_dir / "state.json.tmp"
    tmp.write_text(json.dumps(dataclasses.asdict(t), indent=2))
    os.replace(tmp, ticket_dir / "state.json")


def transition(t: Ticket, to: str, reason: str = "") -> Ticket:
    if to not in STATES:
        raise IllegalTransition(f"unknown state {to!r}")
    allowed = {t.previous} if t.state == "paused" else EDGES[t.state]
    if to not in allowed:
        raise IllegalTransition(f"{t.key}: {t.state} -> {to} not allowed (allowed: {sorted(allowed)})")
    return dataclasses.replace(t, state=to, previous=t.state, reason=reason, updated_utc=_now())


def paused(state_root) -> bool:
    return (pathlib.Path(state_root) / "PAUSE").exists()


def human_owned(ticket_dir) -> bool:
    return (pathlib.Path(ticket_dir) / "HUMAN").exists()


def assign_vendors(index: int) -> tuple:
    return ("fable", "astra") if index % 2 == 1 else ("astra", "fable")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_state -v`
Expected: 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add common/skills/agent-loop/scripts/state.py common/skills/agent-loop/scripts/test_state.py
git commit -m "agent-loop: ticket state machine with new-SHA and pause/resume edges"
```

---

### Task 11: Runner — implement stage end-to-end against a fake worker

**Files:**
- Create: `common/skills/agent-loop/scripts/metrics.py`
- Create: `common/skills/agent-loop/scripts/fake_worker.py`
- Create: `common/skills/agent-loop/scripts/runner.py`
- Test: `common/skills/agent-loop/scripts/test_runner.py`

**Interfaces:**
- `metrics.append(state_root: Path, row: dict) -> None` — appends one JSON line to `metrics.jsonl` under an `flock`; fills `ts_utc` if absent. Row keys used by this plan: `run_id, ticket, task_id, stage, agent, model, tier, arm, attempt, start_utc, end_utc, worker_seconds, outcome, reason, evidence_path, session_dir`. (Plan 2 adds tokens/cost by reading `session_dir`.)
- `runner.Runner(cfg: config.Config, run_id: str, pi_launcher: Callable[[list, Path, float, Path, Path], procs.StageResult] = procs.run_stage)`; `.implement_task(ticket: state.Ticket, task: contracts.Task, task_index: int, worktree_path: Path) -> str` returning the final task outcome `accepted | blocked | paused`; `.run_once(argv) -> int` CLI entry with subcommands `status`, `dry-run --worktree <path> --tasks <tasks.toml> [--scenario <name>]`, `run-once` (reserved: prints "no real tickets in Plan 1" and exits 0).
- Per attempt the runner: (1) checks `PAUSE`/`HUMAN`; (2) `admission.probe`+`decide` (defers up to `defer_max_s` by returning `"paused"` with reason `resource` — no sleeping inside a launchd tick); (3) takes the heavy lease; (4) snapshots; (5) writes `task.md` + `body.md` + `prompt.md` into `attempts/<ticket>/<task_id>/<n>/`; (6) launches `pi` argv via `pi_launcher` in the worktree with timeout `task.timeout_s`; (7) parses `result.md`; (8) computes `changed_paths` and `check_allowlist`; (9) decides outcome: `protocol` (bad result or `STATUS: blocked/REASON: protocol`), `blocked-owner` (`STATUS: blocked/REASON: owner` → task returned to planner, function returns `"blocked"`), `environment` (`REASON: environment` → no rung consumed, gate-only retry is Plan 3; here it counts once then pauses), `timeout`, `rejected` (allowlist violation **or** `STATUS: fail`), `accepted` (`STATUS: pass` and allowlist clean — Plan 3 inserts gates here); (10) on `rejected`/`protocol`: restore snapshot, `append_feedback`; on `timeout` with clean allowlist: keep tree; (11) `metrics.append`; (12) release lease; loop to next rung.
- `fake_worker.py` is a stand-in for `pi`: reads `AL_SCENARIO` env: `pass` (edits first allowed file, writes good result.md), `fail` (edits, writes `STATUS: fail`), `malformed` (edits, writes result.md without STATUS), `escape` (also edits `bin/oops`, claims pass), `tests` (edits `test/x_test.rb`, claims pass), `timeout` (sleeps 30 s), `owner` (writes `STATUS: blocked / REASON: owner`), `env` (`REASON: environment`), `pass_on_feedback` (fails unless `feedback.md` exists, then passes). The task dir and worktree come from `AL_TASK_DIR` / cwd. The runner's test injects a `pi_launcher` that swaps argv[0..1] for `[sys.executable, fake_worker.py]` and sets the env.

- [ ] **Step 1: Write `metrics.py`**

```python
"""Append-only metrics.jsonl. One row per executed stage. Plan 2 adds tokens and cost."""
from __future__ import annotations
import datetime as dt
import fcntl
import json
import pathlib


def append(state_root, row: dict) -> None:
    p = pathlib.Path(state_root) / "metrics.jsonl"
    row = dict(row)
    row.setdefault("ts_utc", dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    with open(p, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(row, sort_keys=True) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)


def read_all(state_root) -> list:
    p = pathlib.Path(state_root) / "metrics.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
```

- [ ] **Step 2: Write `fake_worker.py`**

```python
"""Scenario-driven stand-in for `pi -p` used by the runner tests and `dry-run`.
Invoked as: fake_worker.py <ignored pi args...>; reads AL_SCENARIO and AL_TASK_DIR; cwd = worktree."""
import os, pathlib, sys, time, tomllib

scenario = os.environ.get("AL_SCENARIO", "pass")
task_dir = pathlib.Path(os.environ["AL_TASK_DIR"])
wt = pathlib.Path.cwd()

# The runner writes task.toml (the single task) next to task.md for machine reading.
task = tomllib.loads((task_dir / "task.toml").read_text())
first_allowed = task["allowed_files"][0].split("*")[0].rstrip("/")
target = wt / (first_allowed if first_allowed and not (wt / first_allowed).is_dir() else f"{first_allowed}/worker_touch.rb")
target.parent.mkdir(parents=True, exist_ok=True)


def result(status, reason="none", files=None, nxt="none"):
    (task_dir / "result.md").write_text(
        f"STATUS: {status}\nREASON: {reason}\nBASE: fake\nFILES: {', '.join(files or [])}\n"
        f"EVIDENCE: fake worker scenario={scenario}\nUNVERIFIED: none\nNEXT: {nxt}\n")


if scenario == "timeout":
    time.sleep(30)
    sys.exit(0)
if scenario == "owner":
    result("blocked", "owner", [], "Is the close control part of the header slot?"); sys.exit(0)
if scenario == "env":
    result("fail", "environment", [], "dev DB unreachable"); sys.exit(0)
if scenario == "pass_on_feedback" and not (task_dir / "feedback.md").exists():
    target.write_text("wrong\n"); result("fail", "test", [str(target.relative_to(wt))], "see failing assertion"); sys.exit(0)

target.write_text(f"edited by fake worker ({scenario})\n")
rel = str(target.relative_to(wt))
if scenario == "escape":
    (wt / "bin").mkdir(exist_ok=True); (wt / "bin" / "oops").write_text("x\n"); result("pass", files=[rel, "bin/oops"])
elif scenario == "tests":
    (wt / "test").mkdir(exist_ok=True); (wt / "test" / "x_test.rb").write_text("assert true\n"); result("pass", files=[rel, "test/x_test.rb"])
elif scenario == "malformed":
    (task_dir / "result.md").write_text("I did the thing.\nFILES: " + rel + "\n")
elif scenario == "fail":
    result("fail", "test", [rel], "assertion mismatch")
else:
    result("pass", files=[rel])
```

- [ ] **Step 3: Write the failing test**

```python
# test_runner.py
import json, os, pathlib, subprocess, sys, tempfile, unittest
from unittest.mock import patch
import config, contracts, metrics, procs, runner, state, worktree

HERE = pathlib.Path(__file__).resolve().parent
FAKE = HERE / "fake_worker.py"
TASKS = """
[[tasks]]
id = "001"
slug = "touch"
summary = "touch a file"
allowed_files = ["app/components/**"]
verification_commands = ["true"]
acceptance = ["AC-1"]
"""
AGENT = """---
name: {name}
description: d
model: {model}
thinking: low
tools: read, grep, find, ls, bash, edit, write
---
body
"""

def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, check=True).stdout

class RunnerHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = pathlib.Path(self.tmp.name)
        self.wt = root / "wt"; self.wt.mkdir()
        git(self.wt, "init", "-q", "-b", "main"); git(self.wt, "config", "user.email", "t@t"); git(self.wt, "config", "user.name", "t")
        (self.wt / "app" / "components").mkdir(parents=True); (self.wt / "app" / "components" / ".keep").write_text("")
        git(self.wt, "add", "-A"); git(self.wt, "commit", "-qm", "init")
        agents = root / "agents"; agents.mkdir()
        for n, m in (("cloud-worker", "ollama-cloud/x"), ("local-worker", "ollama-local/x"), ("premium-worker", "openai-codex/x")):
            (agents / f"{n}.md").write_text(AGENT.format(name=n, model=m))
        toml = (HERE.parent / "hopper.toml").read_text()
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
        toml = toml.replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{agents}"')
        toml = toml.replace("heavy_stage_s = 4500", "heavy_stage_s = 3")
        (root / "hopper.toml").write_text(toml)
        self.cfg = config.load(root / "hopper.toml"); self.cfg.ensure_dirs()
        (root / "tasks.toml").write_text(TASKS)
        self.tasks = contracts.load_tasks(root / "tasks.toml")
        self.scenarios = []      # consumed in order, one per launch
        self.launches = 0
        # Admission always green in tests.
        self.adm = patch("runner.admission.probe", return_value=None); self.adm.start()
        self.dec = patch("runner.admission.decide", return_value=runner.admission.Decision(True, [])); self.dec.start()
    def tearDown(self):
        self.adm.stop(); self.dec.stop(); self.tmp.cleanup()

    def launcher(self, argv, cwd, timeout_s, env, stdout_path, stderr_path):
        self.launches += 1
        scenario = self.scenarios.pop(0) if self.scenarios else "pass"
        env = {**(env or os.environ), "AL_SCENARIO": scenario}
        fake_argv = [sys.executable, str(FAKE), *argv[2:]]
        return procs.run_stage(fake_argv, cwd, timeout_s, env, stdout_path, stderr_path)

    def run_task(self, *scenarios, ticket_key="ZIP-7873"):
        self.scenarios = list(scenarios)
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        t = state.load(self.cfg.ticket_dir(ticket_key)); t.worktree = str(self.wt)
        for s in ("spinup", "plan", "plan-review", "implement"):
            t = state.transition(t, s)
        state.save(self.cfg.ticket_dir(ticket_key), t)
        return r.implement_task(t, self.tasks[0], 0, self.wt), metrics.read_all(self.cfg.state_root)

    def test_pass_first_attempt_is_accepted(self):
        outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "accepted"); self.assertEqual(self.launches, 1)
        self.assertEqual(rows[-1]["outcome"], "accepted"); self.assertEqual(rows[-1]["agent"], "cloud-worker")
        self.assertTrue((self.wt / "app" / "components" / "worker_touch.rb").exists())

    def test_fail_then_pass_with_feedback(self):
        outcome, rows = self.run_task("pass_on_feedback", "pass_on_feedback")
        self.assertEqual(outcome, "accepted"); self.assertEqual(self.launches, 2)
        self.assertEqual([r["outcome"] for r in rows], ["rejected", "accepted"])
        self.assertEqual([r["attempt"] for r in rows], [1, 2])
        fb = list(self.cfg.state_root.glob("attempts/**/feedback.md"))
        self.assertTrue(fb and "Attempt 1" in fb[0].read_text())

    def test_ladder_exhaustion_blocks_and_preserves_diffs(self):
        outcome, rows = self.run_task("fail", "fail", "fail")
        self.assertEqual(outcome, "blocked"); self.assertEqual(self.launches, 3)
        self.assertEqual([r["agent"] for r in rows], ["cloud-worker", "cloud-worker", "premium-worker"])
        self.assertEqual([r["tier"] for r in rows], ["cheap", "cheap", "premium"])
        self.assertEqual(git(self.wt, "status", "--porcelain").strip(), "")       # tree restored
        self.assertEqual(len(list(self.cfg.state_root.glob("attempts/ZIP-7873/001/*/diff.patch"))), 3)

    def test_malformed_result_is_protocol_and_advances(self):
        outcome, rows = self.run_task("malformed", "pass")
        self.assertEqual(outcome, "accepted"); self.assertEqual(rows[0]["outcome"], "protocol")

    def test_out_of_allowlist_edit_is_rejected_and_restored(self):
        outcome, rows = self.run_task("escape", "pass")
        self.assertEqual(rows[0]["outcome"], "rejected"); self.assertIn("protected", rows[0]["reason"])
        self.assertFalse((self.wt / "bin" / "oops").exists())
        self.assertEqual(outcome, "accepted")

    def test_test_edit_without_permission_is_rejected(self):
        outcome, rows = self.run_task("tests", "pass")
        self.assertEqual(rows[0]["outcome"], "rejected"); self.assertIn("may_edit_tests", rows[0]["reason"])
        self.assertFalse((self.wt / "test" / "x_test.rb").exists())

    def test_timeout_kills_and_advances_keeping_clean_tree(self):
        outcome, rows = self.run_task("timeout", "pass")
        self.assertEqual(rows[0]["outcome"], "timeout"); self.assertEqual(outcome, "accepted")

    def test_owner_block_returns_blocked_without_consuming_ladder(self):
        outcome, rows = self.run_task("owner")
        self.assertEqual(outcome, "blocked"); self.assertEqual(self.launches, 1)
        self.assertEqual(rows[0]["outcome"], "blocked"); self.assertEqual(rows[0]["reason"], "owner")

    def test_environment_does_not_consume_rung_then_pauses(self):
        outcome, rows = self.run_task("env", "env")
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 2)
        self.assertEqual([r["outcome"] for r in rows], ["environment", "environment"])

    def test_pause_file_stops_before_launch(self):
        (self.cfg.state_root / "PAUSE").touch()
        outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 0)

    def test_human_file_stops_before_launch(self):
        (self.cfg.ticket_dir("ZIP-7873")).mkdir(parents=True, exist_ok=True); (self.cfg.ticket_dir("ZIP-7873") / "HUMAN").touch()
        outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 0)

    def test_heavy_lane_held_by_live_owner_pauses(self):
        import locks
        held = locks.Lease(self.cfg.state_root / "locks" / "heavy", "other"); self.assertTrue(held.acquire())
        outcome, rows = self.run_task("pass")
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 0)

    def test_admission_red_pauses_with_reason(self):
        self.dec.stop()
        with patch("runner.admission.decide", return_value=runner.admission.Decision(False, ["on battery power"])):
            outcome, rows = self.run_task("pass")
        self.dec.start()
        self.assertEqual(outcome, "paused"); self.assertEqual(self.launches, 0)
        t = state.load(self.cfg.ticket_dir("ZIP-7873")); self.assertIn("battery", t.reason)

    def test_local_arm_assigned_for_odd_index(self):
        self.scenarios = ["pass"]
        r = runner.Runner(self.cfg, run_id="test", pi_launcher=self.launcher)
        t = state.load(self.cfg.ticket_dir("ZIP-7873")); t.worktree = str(self.wt)
        for s in ("spinup", "plan", "plan-review", "implement"): t = state.transition(t, s)
        r.implement_task(t, self.tasks[0], 1, self.wt)
        self.assertEqual(metrics.read_all(self.cfg.state_root)[-1]["agent"], "local-worker")

    def test_dry_run_cli(self):
        with patch("runner.procs.run_stage", side_effect=self.launcher):
            rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run",
                              "--worktree", str(self.wt), "--tasks", str(self.cfg.state_root.parent / "tasks.toml"), "--scenario", "pass"])
        self.assertEqual(rc, 0)
        self.assertEqual(metrics.read_all(self.cfg.state_root)[-1]["outcome"], "accepted")

    def test_status_cli_prints_ticket_states(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "status"])
        self.assertEqual(rc, 0); self.assertIn("ZIP-7873", buf.getvalue())

    def test_second_runner_instance_exits_3(self):
        import locks
        other = locks.Lease(self.cfg.state_root / "locks" / "runner", "runner"); self.assertTrue(other.acquire())
        rc = runner.main(["--config", str(self.cfg.state_root.parent / "hopper.toml"), "dry-run",
                          "--worktree", str(self.wt), "--tasks", str(self.cfg.state_root.parent / "tasks.toml")])
        self.assertEqual(rc, 3)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run test to verify it fails**

Run: `python3 -m unittest test_runner -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'runner'`

- [ ] **Step 5: Write `runner.py`**

```python
"""Agent-loop runner. Plan 1 scope: the `implement` stage against the worker ladder, plus
`status` and `dry-run` CLIs. Plans 2 and 3 add ledger enrichment, gates, PR, and review loops."""
from __future__ import annotations
import argparse
import dataclasses
import datetime as dt
import json
import pathlib
import subprocess
import sys
import uuid

import admission
import agentdef
import config
import contracts
import ladder
import locks
import metrics
import procs
import state
import worktree


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_md(task: contracts.Task, task_dir: pathlib.Path, wt: pathlib.Path) -> str:
    def bl(items):
        return "\n".join(f"- {i}" for i in items) or "- (none)"
    return f"""# Task {task.id}: {task.summary}
Task directory: {task_dir}
Worktree: {wt}
Visual evidence required: {"yes" if task.visual else "no"}
Stage timeout: {task.timeout_s // 60} minutes
May edit tests/fixtures: {"yes" if task.may_edit_tests else "no"}

## Allowed files
{bl(task.allowed_files)}

## Invariants
{bl(task.invariants)}

## Out of scope
{bl(task.out_of_scope)}

## Acceptance
{bl(task.acceptance)}

## Verification commands
{bl(task.verification_commands)}

## Stop / escalate when
{bl(task.stop_when)}

If `{task_dir}/feedback.md` exists, read it first. Write `{task_dir}/result.md` when done, even on failure.
Do not commit or push. Never weaken an acceptance check.
"""


def _classify(res: contracts.Result | None, stage: procs.StageResult, violations: list) -> tuple:
    """Return (outcome, reason)."""
    if stage.timed_out:
        return "timeout", "stage timeout"
    if res is None:
        return "protocol", "result.md missing or without STATUS"
    if res.status == "blocked":
        return ("blocked", "owner") if res.reason == "owner" else ("protocol", f"blocked/{res.reason or 'unspecified'}")
    if res.reason == "environment":
        return "environment", res.next or "environment"
    if violations:
        return "rejected", "; ".join(violations)
    if res.status == "pass":
        return "accepted", "none"
    return "rejected", f"{res.status}/{res.reason}: {res.next}"


class Runner:
    def __init__(self, cfg: config.Config, run_id: str | None = None, pi_launcher=procs.run_stage):
        self.cfg = cfg
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.launch = pi_launcher

    # ----- implement stage -------------------------------------------------
    def implement_task(self, t: state.Ticket, task: contracts.Task, task_index: int, wt) -> str:
        wt = pathlib.Path(wt)
        tdir = self.cfg.ticket_dir(t.key)
        spec = self.cfg.tickets.get(t.key)
        arm = ladder.assign_arm(task_index, task.visual, spec.pin_arm if spec else None, self.cfg.arms_alternate)
        attempts = [ladder.Attempt(ladder.Rung(**a["rung"]), a["outcome"]) for a in t.attempts.get(task.id, [])]
        env_failures = 0

        while True:
            rung = ladder.next_rung(attempts, arm)
            if rung is None:
                if attempts and attempts[-1].outcome == "accepted":
                    return "accepted"
                self._save(tdir, state.transition(t, "blocked", reason=f"task {task.id}: ladder exhausted"))
                return "blocked"

            if state.paused(self.cfg.state_root):
                self._save(tdir, state.transition(t, "paused", reason="PAUSE file present")); return "paused"
            if state.human_owned(tdir):
                self._save(tdir, state.transition(t, "paused", reason="HUMAN file present")); return "paused"
            reading = admission.probe(self.cfg.state_root)
            decision = admission.decide(reading, self.cfg.admission)
            if not decision.ok:
                self._save(tdir, state.transition(t, "paused", reason="resource: " + "; ".join(decision.reasons))); return "paused"

            lease = locks.Lease(self.cfg.state_root / "locks" / "heavy", f"implement {t.key}/{task.id}")
            if not lease.acquire():
                self._save(tdir, state.transition(t, "paused", reason="heavy lane held by a live owner")); return "paused"
            try:
                n = len(attempts) + 1
                outcome, reason, row = self._attempt(t, task, wt, arm, rung, n)
            finally:
                lease.release()

            attempts.append(ladder.Attempt(rung, outcome))
            t.attempts.setdefault(task.id, []).append({"rung": dataclasses.asdict(rung), "outcome": outcome, "reason": reason, "n": n})
            self._save(tdir, t)
            metrics.append(self.cfg.state_root, row)

            if outcome == "accepted":
                return "accepted"
            if outcome == "blocked":
                self._save(tdir, state.transition(t, "blocked", reason=f"task {task.id}: {reason}")); return "blocked"
            if outcome == "environment":
                env_failures += 1
                if env_failures >= 2:
                    self._save(tdir, state.transition(t, "paused", reason=f"task {task.id}: repeated environment failure: {reason}")); return "paused"
            if outcome in ("rejected", "protocol"):
                ladder.append_feedback(self._attempt_root(t, task), n, reason)

    def _attempt_root(self, t, task) -> pathlib.Path:
        return self.cfg.state_root / "attempts" / t.key / task.id

    def _attempt(self, t, task, wt, arm, rung, n):
        adir = self._attempt_root(t, task) / str(n)
        adir.mkdir(parents=True, exist_ok=True)
        # feedback.md lives at the task level so every attempt sees the accumulated history
        task_level = self._attempt_root(t, task)
        (adir / "task.toml").write_text(_task_toml(task))
        (adir / "task.md").write_text(_task_md(task, task_level, wt))
        if (task_level / "feedback.md").exists():
            (adir / "feedback.md").write_text((task_level / "feedback.md").read_text())
        agent = agentdef.load(self.cfg.pi_agents_dir, rung.agent)
        agentdef.assert_worker_safe(agent)
        (adir / "body.md").write_text(agent.body)
        prompt = adir / "prompt.md"
        prompt.write_text(f"Your task file is {adir / 'task.md'}. Read it, then begin. Write result.md to {adir}.\n")
        base = worktree.snapshot(wt)
        (adir / "base_tree").write_text(base)
        argv = agentdef.pi_argv(agent, prompt, adir / "session", adir / "body.md")
        env = {**__import__("os").environ, "AL_TASK_DIR": str(adir), "AL_TICKET": t.key, "AL_TASK": task.id}
        start = _now()
        stage = self.launch(argv, wt, float(task.timeout_s), env, adir / "stdout.log", adir / "stderr.log")
        end = _now()

        res = None
        try:
            res = contracts.parse_result((adir / "result.md").read_text())
        except (FileNotFoundError, contracts.ProtocolError):
            res = None
        changed = worktree.changed_paths(wt, base)
        violations = worktree.check_allowlist(changed, task, self.cfg.protected_paths, self.cfg.test_path_globs)
        outcome, reason = _classify(res, stage, violations)

        # Preserve the diff for every attempt, then decide whether the tree keeps it.
        diff = subprocess.run(["git", "-C", str(wt), "diff", base, worktree.snapshot(wt)], capture_output=True, text=True).stdout
        (adir / "diff.patch").write_text(diff)
        if outcome in ("rejected", "protocol", "blocked", "environment") or (outcome == "timeout" and violations):
            worktree.restore(wt, base)

        row = {"run_id": self.run_id, "ticket": t.key, "task_id": task.id, "stage": "implement",
               "agent": rung.agent, "model": agent.model, "tier": rung.tier, "arm": arm, "attempt": n,
               "start_utc": start, "end_utc": end, "worker_seconds": round(stage.elapsed_s, 2),
               "outcome": outcome, "reason": reason, "evidence_path": str(adir), "session_dir": str(adir / "session"),
               "changed_paths": changed}
        return outcome, reason, row

    def _save(self, tdir, t):
        state.save(tdir, t)

    # ----- CLIs --------------------------------------------------------------
    def status(self) -> int:
        tickets_dir = self.cfg.state_root / "tickets"
        heavy = locks.owner(self.cfg.state_root / "locks" / "heavy")
        print(f"epic {self.cfg.epic}  paused={state.paused(self.cfg.state_root)}  heavy_lane={'held by ' + str(heavy['pid']) if heavy else 'free'}")
        for key in self.cfg.tickets:
            t = state.load(tickets_dir / key)
            human = " HUMAN" if state.human_owned(tickets_dir / key) else ""
            print(f"  {key:<10} {t.state:<14} {t.reason}{human}")
        return 0


def _task_toml(task: contracts.Task) -> str:
    def arr(xs):
        return "[" + ", ".join(json.dumps(x) for x in xs) + "]"
    return (f'id = {json.dumps(task.id)}\nslug = {json.dumps(task.slug)}\nsummary = {json.dumps(task.summary)}\n'
            f'allowed_files = {arr(task.allowed_files)}\nverification_commands = {arr(task.verification_commands)}\n'
            f'acceptance = {arr(task.acceptance)}\nmay_edit_tests = {str(task.may_edit_tests).lower()}\n'
            f'visual = {str(task.visual).lower()}\ntimeout_s = {task.timeout_s}\n')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent-loop")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("run-once")
    d = sub.add_parser("dry-run")
    d.add_argument("--worktree", required=True); d.add_argument("--tasks", required=True)
    d.add_argument("--scenario", default="pass"); d.add_argument("--ticket", default="DRY-1")
    a = ap.parse_args(argv)
    cfg = config.load(a.config); cfg.ensure_dirs()
    r = Runner(cfg)
    if a.cmd == "status":
        return r.status()
    # Global runner lease: two launchd ticks (or a tick plus a manual run) must never overlap.
    runner_lease = locks.Lease(cfg.state_root / "locks" / "runner", "runner")
    if not runner_lease.acquire():
        print("another runner instance is live; exiting", file=sys.stderr); return 3
    try:
        if a.cmd == "run-once":
            print("run-once: real ticket execution lands in Plan 3; nothing to do", file=sys.stderr); return 0
        import os
        os.environ["AL_SCENARIO"] = a.scenario
        tasks = contracts.load_tasks(a.tasks)
        t = state.load(cfg.ticket_dir(a.ticket)); t.worktree = a.worktree
        if t.state == "queued":
            for s in ("spinup", "plan", "plan-review", "implement"):
                t = state.transition(t, s)
            state.save(cfg.ticket_dir(a.ticket), t)
        for i, task in enumerate(tasks):
            out = r.implement_task(t, task, i, a.worktree)
            print(f"{a.ticket} task {task.id}: {out}")
            if out != "accepted":
                return 1
        return 0
    finally:
        runner_lease.release()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m unittest test_runner -v`
Expected: 17 tests PASS (timeout test takes ~3 s)

- [ ] **Step 7: Run the whole agent-loop suite**

Run: `python3 -m unittest discover -p 'test_*.py' -v 2>&1 | tail -5`
Expected: all tests PASS, `OK`

- [ ] **Step 8: Commit**

```bash
git add common/skills/agent-loop/scripts/metrics.py common/skills/agent-loop/scripts/fake_worker.py common/skills/agent-loop/scripts/runner.py common/skills/agent-loop/scripts/test_runner.py
git commit -m "agent-loop: runner implement stage with ladder, allowlist, snapshots, leases, metrics"
```

---

### Task 12: Live `cmux_chain.py` — headless chain and epic exclusion (dotfiles repo)

**Files:**
- Modify: `~/.claude/skills/spinup/scripts/cmux_chain.py` (`run_chain`, `cmd_cmux_poll`)
- Modify: `~/.claude/skills/spinup/scripts/spinup_helper.py` (`list_assigned_eligible`)
- Test: `~/.claude/skills/spinup/scripts/test_cmux_chain.py`, `test_spinup_helper.py`

**Interfaces:**
- Produces: `run_chain(..., harness=None)` → no agent tab; result gains `"agent_surface": None`. `list_assigned_eligible()` rows gain `"parent": "<KEY>" | ""`. `cmd_cmux_poll` skips tickets whose `parent` is in `runner_owned_epics()`, which reads `[hopper].owned_epics` from `AGENT_LOOP_HOPPER` env or `~/workspace/agentfiles/common/skills/agent-loop/hopper.toml` if it exists, else `[]`.

- [ ] **Step 1: Confirm dotfiles repo state**

Run: `cd ~/.config/lnk/Mac.lan.lnk && git status --short -- .claude/skills/spinup && git log --oneline -1`
Expected: no pending changes under `.claude/skills/spinup`. (Other files may be dirty; leave them alone and stage only the three spinup files.)

- [ ] **Step 2: Write the failing tests**

Append to `test_cmux_chain.py`:

```python
class HeadlessChainTests(unittest.TestCase):
    @patch("cmux_chain.open_ref_browser_pane")
    @patch("cmux_chain.open_browser_tab")
    @patch("cmux_chain.open_agent_tab")
    @patch("cmux_chain.wait_for_dev_server", return_value=True)
    @patch("cmux_chain.open_dev_server_tab")
    @patch("cmux_chain.cmux_focus_workspace")
    @patch("cmux_chain.port_in_use", return_value=False)
    @patch("cmux_chain.read_worktree_port", return_value=3062)
    @patch("cmux_chain.record_workspace_map")
    @patch("cmux_chain.wait_for_setup", return_value=True)
    @patch("cmux_chain.open_setup_tab", return_value="workspace:9")
    @patch("cmux_chain.ensure_worktree", return_value="/wt")
    @patch("cmux_chain.cmux_notify")
    def test_harness_none_skips_agent_tab(self, notify, ew, ost, wfs, rwm, rwp, piu, focus, odst, wfds, oat, obt, orbp):
        r = cc.run_chain(name="zip-1", branch="b", new_branch=True, prompt="", harness=None)
        oat.assert_not_called()
        obt.assert_called_once()
        self.assertEqual(r["status"], "ok"); self.assertIsNone(r.get("agent_surface"))

class OwnedEpicExclusionTests(unittest.TestCase):
    def test_runner_owned_epics_reads_toml(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[hopper]\nepic="ZIP-1"\nowned_epics=["ZIP-1","ZIP-2"]\n'); p = f.name
        with patch.dict(os.environ, {"AGENT_LOOP_HOPPER": p}):
            self.assertEqual(cc.runner_owned_epics(), ["ZIP-1", "ZIP-2"])
        os.unlink(p)
    def test_runner_owned_epics_empty_when_missing(self):
        import os
        with patch.dict(os.environ, {"AGENT_LOOP_HOPPER": "/nonexistent/hopper.toml"}):
            self.assertEqual(cc.runner_owned_epics(), [])

    @patch("cmux_chain.within_work_hours", return_value=True)
    @patch("cmux_chain.cmux_notify")
    @patch("spinup_helper.list_review_requests", return_value=[])
    @patch("spinup_helper.mark_spunup")
    @patch("cmux_chain.run_chain", return_value={"status": "ok", "workspace": "workspace:3", "worktree": "/wt"})
    @patch("spinup_helper.transition_to_in_progress", return_value=True)
    @patch("cmux_chain.runner_owned_epics", return_value=["ZIP-6774"])
    @patch("spinup_helper.list_assigned_eligible",
           return_value=[{"key": "ZIP-7873", "title": "Well", "issue_type": "Task", "status": "Selected for Work", "parent": "ZIP-6774"},
                         {"key": "ZIP-9", "title": "T", "issue_type": "Bug", "status": "Triage", "parent": ""}])
    def test_poll_skips_runner_owned_children(self, elig, owned, trans, chain, mark, prs, notify, wh):
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            cc.cmd_cmux_poll(type("A", (), {})())
        spun = [c.kwargs.get("name") or c[0][0] for c in chain.call_args_list]
        self.assertEqual(spun, ["zip-9"])
```

Append to `test_spinup_helper.py`:

```python
class EligibleParentTests(unittest.TestCase):
    @patch("spinup_helper.subprocess.run")
    def test_list_assigned_eligible_includes_parent_key(self, run):
        m = MagicMock(); m.returncode = 0; m.stdout = json.dumps([
            {"key": "ZIP-7873", "fields": {"summary": "Well", "issuetype": {"name": "Task"}, "status": {"name": "Selected for Work"}, "parent": {"key": "ZIP-6774"}}},
            {"key": "ZIP-9", "fields": {"summary": "T", "issuetype": {"name": "Bug"}, "status": {"name": "Triage"}}},
        ]); run.return_value = m
        rows = sh.list_assigned_eligible()
        self.assertEqual(rows[0]["parent"], "ZIP-6774"); self.assertEqual(rows[1]["parent"], "")
        self.assertIn("parent", run.call_args[0][0][run.call_args[0][0].index("--fields") + 1])
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ~/.claude/skills/spinup/scripts && python3 -m unittest test_cmux_chain.HeadlessChainTests test_cmux_chain.OwnedEpicExclusionTests test_spinup_helper.EligibleParentTests -v`
Expected: FAIL — `run_chain() got an unexpected keyword 'harness'`? No: `harness` exists; expect `AttributeError: module 'cmux_chain' has no attribute 'runner_owned_epics'` and an `AssertionError` on `oat.assert_not_called()`, and `KeyError: 'parent'`.

- [ ] **Step 4: Implement in `spinup_helper.py`**

In `list_assigned_eligible`, change the `acli` argv to request the parent field and emit it:

```python
    result = subprocess.run(
        ["acli", "jira", "workitem", "search", "--jql", jql, "--limit", "50", "--json",
         "--fields", "issuetype,key,assignee,priority,status,summary,parent"],
        capture_output=True, text=True,
    )
    ...
        out.append({
            "key": i.get("key"),
            "title": fields.get("summary", ""),
            "issue_type": (fields.get("issuetype") or {}).get("name", ""),
            "status": (fields.get("status") or {}).get("name", ""),
            "parent": (fields.get("parent") or {}).get("key", ""),
        })
```

- [ ] **Step 5: Implement in `cmux_chain.py`**

Add after the constants block:

```python
import tomllib

AGENT_LOOP_HOPPER_DEFAULT = Path.home() / "workspace" / "agentfiles" / "common" / "skills" / "agent-loop" / "hopper.toml"


def runner_owned_epics() -> list:
    """Epics whose children the agent-loop runner owns; the listener must not spin them."""
    p = Path(os.environ.get("AGENT_LOOP_HOPPER", AGENT_LOOP_HOPPER_DEFAULT))
    try:
        with open(p, "rb") as f:
            return list(tomllib.load(f).get("hopper", {}).get("owned_epics", []))
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return []
```

In `run_chain`, change the signature default and the agent-tab block:

```python
def run_chain(name: str, branch: str, new_branch: bool, prompt: str, ref_url: str = None,
              group: dict = None, harness: str | None = DEFAULT_HARNESS) -> dict:
    """... harness=None opens no agent tab (the agent-loop runner dispatches its own workers)."""
    ...
    cmux_focus_workspace(workspace)  # re-assert again after the dev-server wait
    agent_surface = open_agent_tab(workspace, prompt, worktree, agent=harness) if harness else None
    if serving:
        open_browser_tab(workspace, name)
        cmux_notify(f"{name} ready", "plan running" if harness else "runner-owned", workspace=workspace)
        status = "ok"
    else:
        cmux_notify(f"{name}: dev server slow", "agent started; open browser manually" if harness else "runner-owned; open browser manually",
                    workspace=workspace)
        status = "serving-timeout"

    if ref_url:
        open_ref_browser_pane(workspace, ref_url)

    return {"status": status, "workspace": workspace, "worktree": worktree, "agent_surface": agent_surface}
```

In `cmd_cmux_poll`, inside the `for t in eligible:` loop, immediately after the `already` check:

```python
            if t.get("parent") in runner_owned_epics():
                print(f"skip {t['key']}: child of runner-owned epic {t['parent']}", file=sys.stderr)
                continue
```

- [ ] **Step 6: Run the full dotfiles spinup suite**

Run: `python3 -m unittest test_cmux_chain test_spinup_helper -v 2>&1 | tail -4`
Expected: all PASS, `OK`. If a pre-existing `run_chain` test asserts on the exact returned dict, update it to include `"agent_surface"`.

- [ ] **Step 7: Commit in the dotfiles repo (only these three files)**

```bash
cd ~/.config/lnk/Mac.lan.lnk
git add .claude/skills/spinup/scripts/cmux_chain.py .claude/skills/spinup/scripts/spinup_helper.py .claude/skills/spinup/scripts/test_cmux_chain.py .claude/skills/spinup/scripts/test_spinup_helper.py
git commit -m "spinup: headless run_chain(harness=None) and skip agent-loop-owned epics in cmux-poll"
```

---

### Task 13: Operator docs and launchd source

**Files:**
- Create: `common/skills/agent-loop/SKILL.md`
- Create: `common/skills/agent-loop/inc.example.agent-loop.plist`
- Create: `common/skills/agent-loop/scripts/agent-loop-tick.sh`

- [ ] **Step 1: Write `SKILL.md`**

```markdown
---
name: agent-loop
description: Operate the autonomous ZIP-6774 agent loop -- status, pause/resume, human takeover of a ticket, dry runs. Use when Cody asks about the loop, the hopper, worker attempts, or wants to stop/take over autonomous work.
---

# /agent-loop

Operator surface for the runner in `scripts/runner.py`. Design:
`docs/superpowers/specs/2026-09-10-zip-6774-agent-loop-design.md`.

## Commands

All run from anywhere; `--config` defaults to this skill's `hopper.toml`.

- **Status** -- `python3 ~/.pi/agent/skills/agent-loop/scripts/runner.py status`
  Prints pause state, heavy-lane holder, and each hopper ticket's state and reason.
- **Pause everything** -- `touch ~/.local/state/agent-loop/PAUSE`. No new stage starts;
  the current atomic step completes. Resume with `rm`.
- **Take over one ticket** -- `touch ~/.local/state/agent-loop/tickets/<KEY>/HUMAN`.
  The runner finishes its current step, releases the heavy lane, and never touches that
  worktree again until the file is removed. Two writers never share a tree.
- **Dry run** (no models, no Jira, fake worker):
  `AL_SCENARIO=pass python3 .../runner.py dry-run --worktree <path> --tasks <tasks.toml> --scenario pass`
  Scenarios: `pass fail malformed escape tests timeout owner env pass_on_feedback`.
  Requires the fake worker to be substituted for `pi`; see `test_runner.py` for the launcher hook.
  In this plan the dry-run CLI uses the real `pi` argv unless patched -- it exists to exercise
  state, locks, and metrics plumbing on a scratch worktree, not to call models.

## State on disk

`~/.local/state/agent-loop/` (mode 0700):
`tickets/<KEY>/state.json`, `attempts/<KEY>/<task>/<n>/{task.md,task.toml,prompt.md,body.md,result.md,diff.patch,base_tree,stdout.log,stderr.log,session/}`,
`attempts/<KEY>/<task>/feedback.md`, `locks/heavy`, `metrics.jsonl`, `PAUSE`.

## What is NOT wired yet (Plans 2-3)

Ledger token/cost enrichment and dashboard; gates (lint, tests, Playwright, Figma, lens-review);
draft PR, ready, bot/colleague loops; real ticket selection (`run-once`); launchd tick going live.
```

- [ ] **Step 2: Write `scripts/agent-loop-tick.sh`**

```bash
#!/bin/bash
# launchd entrypoint for the agent-loop idle tick. Bare env -> explicit PATH. Secrets come from
# Keychain via models.json, never from this file or the plist.
export HOME="${HOME:-/Users/cody}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin"
exec /opt/homebrew/bin/python3 "$HOME/.pi/agent/skills/agent-loop/scripts/runner.py" run-once \
  >> "$HOME/.local/state/agent-loop/tick.log" 2>&1
```

Run: `chmod +x common/skills/agent-loop/scripts/agent-loop-tick.sh`

- [ ] **Step 3: Write `inc.example.agent-loop.plist`** (source of truth; **not** installed by this plan)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>inc.example.agent-loop</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string>
    <string>/Users/cody/.pi/agent/skills/agent-loop/scripts/agent-loop-tick.sh</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>/Users/cody/.local/state/agent-loop/launchd.out.log</string>
  <key>StandardErrorPath</key><string>/Users/cody/.local/state/agent-loop/launchd.err.log</string>
</dict></plist>
```

Run: `plutil -lint common/skills/agent-loop/inc.example.agent-loop.plist`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add common/skills/agent-loop/SKILL.md common/skills/agent-loop/inc.example.agent-loop.plist common/skills/agent-loop/scripts/agent-loop-tick.sh
git commit -m "agent-loop: operator SKILL.md, tick wrapper, launchd plist source (not loaded)"
```

---

### Task 14: Slice-1 activation prerequisites (human-in-loop, no commits)

This task produces **measurements and decisions**, recorded in `~/.local/state/agent-loop/slice1-notes.md`, and updates the two placeholder model IDs in `common/model-tiers.toml` (Task 8) if the probes choose different models. Nothing here is unattended.

- [ ] **Step 1: Render the worker agents into pi and verify no fallback leaked**

Run: `cd ~/workspace/agentfiles && ./bootstrap.sh --tool pi` then `for a in cloud-worker local-worker premium-worker; do echo "--- $a"; sed -n '1,8p' ~/.pi/agent/agents/$a.md; done | rg -n 'model:|thinking:|tools:|fallback' `
Expected: each shows `model:` from its tier table, `thinking:`, `tools: read, grep, find, bash, write, edit`, and **no** `fallbackModels` line. If the renderer injects `fallbackModels` from settings for all agents, stop and report — that is a spec-level conflict to resolve with Cody before any worker runs.

- [ ] **Step 2: Keychain-backed Ollama Cloud key** (Cody supplies the key)

Run: `security add-generic-password -a "$USER" -s ollama-cloud -w` (prompts for the key) then `security find-generic-password -ws ollama-cloud | wc -c`
Expected: a non-zero character count. Never echo the key.

- [ ] **Step 3: Add both providers to `~/.pi/agent/models.json`**

Merge into the existing `providers` object (keep the `openai-codex` overrides):

```json
"ollama-cloud": {
  "baseUrl": "https://ollama.com/v1",
  "api": "openai-completions",
  "apiKey": "!security find-generic-password -ws ollama-cloud",
  "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
  "models": [
    { "id": "deepseek-v4-flash", "reasoning": true, "contextWindow": 128000, "maxTokens": 32000,
      "cost": { "input": 0.22, "output": 0.66, "cacheRead": 0.007, "cacheWrite": 0 } },
    { "id": "glm-5.3-flash", "reasoning": true, "contextWindow": 128000, "maxTokens": 32000,
      "cost": { "input": 0.15, "output": 0.50, "cacheRead": 0.03, "cacheWrite": 0 } },
    { "id": "gpt-oss:20b", "reasoning": true, "contextWindow": 128000, "maxTokens": 32000,
      "cost": { "input": 0.07, "output": 0.30, "cacheRead": 0.035, "cacheWrite": 0 } }
  ]
},
"ollama-local": {
  "baseUrl": "http://localhost:11434/v1",
  "api": "openai-completions",
  "apiKey": "ollama",
  "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
  "models": [
    { "id": "gpt-oss:20b", "reasoning": true, "contextWindow": 128000, "maxTokens": 32000,
      "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 } }
  ]
}
```

Run: `pi --list-models 2>/dev/null | rg 'ollama-(cloud|local)'`
Expected: six rows (three cloud, one local, plus any duplicates by name). Context windows are placeholders; correct them from the probe output below.

- [ ] **Step 4: Cloud probe — which candidates does the Free plan serve, and do they call tools**

For each of `deepseek-v4-flash`, `glm-5.3-flash`, `gpt-oss:20b`:

Run: `mkdir -p /tmp/al-probe && cd /tmp/al-probe && git init -q 2>/dev/null; printf 'Create a file named hello.txt containing exactly the word hello, then reply DONE.' > p.md && time pi -p --no-session --model ollama-cloud/<id>:medium --tools read,write,ls "@/tmp/al-probe/p.md" 2>&1 | tail -3; cat hello.txt`
Record in `slice1-notes.md`: served or refused (HTTP status / message), wall time, whether `hello.txt` exists with `hello`, and whether the JSON/session shows `usage` fields when run once more **with** `--session-dir /tmp/al-probe/sess` (then `rg -c '"usage"' /tmp/al-probe/sess/*.jsonl`). A model that is refused is dropped from `[worker-cloud]` candidates; a model that returns no usage is a **stop condition** for that model per the spec.

- [ ] **Step 5: Cloud concurrency probe**

Run two of the Step 4 commands simultaneously (`... & ...; wait`) and record whether the second queues, errors with 429, or is rejected — this confirms the 1-concurrent-request rule the runner assumes.

- [ ] **Step 6: Record the starter credit**

Open `https://ollama.com/settings/usage` (or the usage page linked from pricing) and record the starter credit amount and reset date in `slice1-notes.md`. Set `cloud_starter_credit_usd` in `hopper.toml` accordingly (commit that one-line change in agentfiles).

- [ ] **Step 7: Local Ollama install and residency probe**

Run: `brew install ollama && brew services start ollama && ollama pull gpt-oss:20b && ollama show gpt-oss:20b | rg -i 'parameters|quantization|context'`
Then the same probe as Step 4 with `--model ollama-local/gpt-oss:20b:medium`, while in a second terminal sampling: `for i in $(seq 1 12); do ollama ps; vm_stat | rg 'compressor'; pmset -g therm | rg CPU_Speed; sleep 10; done`.
Record: resident size from `ollama ps`, load time (first token), peak compressor pages, `CPU_Speed_Limit` minimum, wall time, tool-call success. **Budget check:** if resident size > ~10 GB or `CPU_Speed_Limit` drops below 100 during a five-minute session, try one smaller candidate (a 14B Q4 coder from the Ollama library; record which), else drop the local arm for the pilot and record why.
Then: `OLLAMA_KEEP_ALIVE=0` and `OLLAMA_MAX_LOADED_MODELS=1` into the service env (`brew services` env file or a `launchctl setenv` documented in `slice1-notes.md`), restart, and confirm `ollama ps` is empty 5 s after a request completes. Finally `brew services stop ollama` — the daemon runs only when the hopper is active (Plan 3 starts/stops it).

- [ ] **Step 8: Update tier tables if the probes changed the choice**

If `[worker-cloud].pi_model` or `[worker-local].pi_model` differ from the probe winners, edit `common/model-tiers.toml`, re-run `test_agent_sources`, re-render with `bootstrap.sh --tool pi`, and commit: `git commit -am "agent-loop: worker model IDs from slice-1 probes"`.

- [ ] **Step 9: Locate the `create-pull-request` skill**

Run: `for d in ~/.pi/agent/skills ~/.claude/skills ~/.agents/skills ~/.codex/skills ~/workspace/zipline-app/.claude/skills ~/workspace/zipline-app/.claude/commands; do ls "$d" 2>/dev/null | rg -i 'pull-request|create-pr' && echo "  in $d"; done; rg -rn 'CLAUDE_PR_SKILL' ~/.claude/settings.json ~/.claude/hooks 2>/dev/null | head -3`
Record where it is (or that it is absent) in `slice1-notes.md`. If absent, this is the first item Cody must decide on before Plan 3: restore it from the Zipline repo / plugin marketplace, or author it. Do **not** work around the hook.

- [ ] **Step 10: Headless chain smoke on a scratch branch**

Run: `cd ~/.claude/skills/spinup/scripts && python3 -c "import cmux_chain as cc; print(cc.run_chain(name='al-smoke', branch='al-smoke', new_branch=True, prompt='', harness=None))"`
Expected: a workspace with setup, dev-server, and browser tabs and **no** agent tab; `agent_surface: None`. Then tear down: `python3 cmux_chain.py spindown al-smoke`. Record outcome.

- [ ] **Step 11: Write up**

`slice1-notes.md` must answer: which cloud models are served; usage metadata present per model; concurrency behavior; starter credit; local model chosen or arm dropped, with numbers; `create-pull-request` location; headless chain OK. Hand the file to Cody. **Plan 2 does not start until Cody has read it**, because the fork on "buy credits vs. local+premium only" is his.

---

## Self-Review

**1. Spec coverage (slices 1–2 and the sections they depend on):**

| Spec requirement | Task |
| --- | --- |
| `ollama-cloud` / `ollama-local` providers, Keychain key, compat flags | 14 |
| Worker agent defs: no fallback, tool allowlist, shared contract body | 7, 8, 14.1 |
| Tier tables incl. flagship pair | 8 |
| Hopper dependency graph, pins, pre-registered outcomes | 1 |
| Runner is scheduler of record; listener excludes owned epics | 12 |
| `run_chain` without agent tab | 12, 14.10 |
| Task manifest schema, runner validates and never slices | 5, 11 (`load_tasks`) |
| Worker result contract, lenient/strict parsing | 5 |
| Diff allowlist incl. protected + test paths | 6, 11 |
| Per-attempt snapshots; restore on reject | 6, 11 |
| Ladder B, arm alternation, feedback file, environment does not consume a rung | 9, 11 |
| Heavy lane lease; PID/boot-ID reclaim only | 2, 11 |
| Admission thresholds; defer → pause with reason | 3, 11 |
| Process groups, verified kill | 4 |
| `PAUSE` / `HUMAN` files | 10, 11 |
| State machine incl. new-SHA → gates, pause → previous | 10 |
| Metrics row per stage (tokens/cost deferred to Plan 2) | 11 |
| launchd PATH wrapper, plist source not loaded | 13 |
| Slice-1 probes: served models, usage metadata, concurrency, credit, local residency, PR skill | 14 |
| Two runner instances cannot coexist (global runner lease) | 11 (`main()` lease; found as a gap in self-review and folded into the task) |

Deferred by design to Plans 2–3 (not gaps): ledger tokens/cost, dashboard, gates, PR path, review loops, CI classification, `github_write()`, Jira transition call, wake lease (`caffeinate`), real ticket selection, budget enforcement, cmux notify integration, external-write idempotency.

**2. Placeholder scan:** No "TBD/TODO/similar to Task N" in step bodies. Task 8's model IDs are explicitly labeled placeholders resolved by Task 14 with a concrete update step. Task 7 Step 5 names the exact fallback if `@file` is unsupported.

**3. Type consistency:** `config.Config` field names used in `runner.py` (`state_root`, `pi_agents_dir`, `arms_alternate`, `protected_paths`, `test_path_globs`, `tickets[...].pin_arm`, `admission`) match Task 1. `contracts.Task` fields used by `_task_md`, `_task_toml`, `fake_worker.py`, and `worktree.check_allowlist` match Task 5. `ladder.Rung(agent, tier, n)` / `Attempt(rung, outcome)` match between Tasks 9 and 11; `dataclasses.asdict(rung)` round-trips via `Rung(**a["rung"])`. `procs.run_stage(argv, cwd, timeout_s, env, stdout_path, stderr_path)` signature matches the test launcher and `agentdef.pi_argv` output is consumed positionally. `state.transition` edge table permits every sequence Task 11 exercises (`implement → blocked|paused`). `metrics.append(state_root, row)` matches. Outcome vocabulary is consistent across `ladder.ADVANCING`, `_classify`, and tests: `accepted, rejected, timeout, protocol, environment, blocked` (`unusable` is reserved for Plan 3's gate stage and appears only in `ADVANCING`).
