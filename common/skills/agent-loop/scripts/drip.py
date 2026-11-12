"""Serialized drip: keep `max_in_flight` tickets moving, fed from the epic's Jira board in Rank order.

Selection (deterministic, no model):
  1. Children of `[drip].epic` whose status category is not Done, ordered by board Rank (top first).
  2. Skip: excluded in hopper.toml (with reason); already in flight (has a worktree / ticket state
     past `queued`); any `deps` in hopper.toml not Done in Jira (no autonomous PR stacks); optionally
     tickets assigned to someone else.
  3. Take the first remaining ticket. Spin it up through Cody's normal cmux chain (all panes; the
     agent pane runs /loop-watch), assign it to Cody + transition to In Progress, start its launchd
     supervisor. Repeat until in-flight == max_in_flight.

In flight = spun up and not yet at human-gate-1/done/blocked-for-owner. Tickets at the gate do not
count: they are waiting on Cody, not on the machine.

Runs as a launchd job every 15 min (`runner.py supervise drip-start`), or once (`runner.py drip`).
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import time

import cohort
import recovery
import state
import workspace_binding
import workspaces

IN_FLIGHT_STATES = {"spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready", "bot-loop", "paused"}
TERMINAL = {"human-gate-1", "done", "blocked", "colleague-loop", "human-gate-2"}
WORKTREES = pathlib.Path("~/workspace/example-worktrees").expanduser()
SPINUP_SCRIPTS = pathlib.Path("~/.claude/skills/spinup/scripts").expanduser()
example_APP = pathlib.Path("~/workspace/example-app").expanduser()
ME = "example"
HARD_MAX_IN_FLIGHT = 7             # Cody, 2026-10-08: never more than 7 tickets processing


def cohort_enabled(cfg) -> bool:
    return (cfg.state_root / "cohort.json").exists()


def limit(cfg) -> int:
    if cohort_enabled(cfg):
        return cohort.load(cfg).capacity
    return min(int(cfg.drip.get("max_in_flight", 3)), HARD_MAX_IN_FLIGHT)


READY_STATUSES = {"Selected for Work", "To Do", "Ready", "Ready for Development"}


def _acli_json(args: list[str]):
    r = subprocess.run(["acli", *args, "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode or not r.stdout.strip():
        raise RuntimeError(f"acli {' '.join(args[:3])} failed: {r.stderr.strip()[:200]}")
    d = json.loads(r.stdout)
    return d if isinstance(d, list) else d.get("issues", d.get("values", []))


def board(cfg) -> list[dict]:
    """Children not Done of each `[drip].epics` entry, epic by epic in that order, each in board Rank order."""
    d = cfg.drip
    epics = d.get("epics") or [d.get("epic", cfg.epic)]
    types = d.get("issue_types") or []
    out, seen = [], set()
    for epic in epics:
        jql = f"parent = {epic} AND statusCategory != Done"
        if types:
            jql += " AND issuetype in (" + ",".join(f'"{t}"' for t in types) + ")"
        if d.get("jql_extra"):
            jql += f" AND ({d['jql_extra']})"
        jql += " ORDER BY Rank ASC"
        for r in _acli_json(["jira", "workitem", "search", "--jql", jql, "--fields", "key,summary,status,assignee,issuetype"]):
            if r["key"] in seen:
                continue
            seen.add(r["key"])
            out.append({"key": r["key"], "summary": r["fields"]["summary"], "status": r["fields"]["status"]["name"],
                        "assignee": ((r["fields"].get("assignee") or {}).get("emailAddress") or (r["fields"].get("assignee") or {}).get("displayName") or None)})
    return out


def done_in_jira(keys: list[str]) -> set[str]:
    if not keys:
        return set()
    rows = _acli_json(["jira", "workitem", "search", "--jql", f"key in ({','.join(keys)}) AND statusCategory = Done", "--fields", "status"])
    return {r["key"] for r in rows}


def _park_order(mark: pathlib.Path):
    metadata = mark.with_name("PARKED.json")
    try:
        value = json.loads(metadata.read_text())
        sequence = int(value["admission_sequence"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        sequence = 10**12
    return sequence, mark.stat().st_mtime, mark.parent.name


def parked(cfg) -> list[str]:
    """Parked tickets, oldest durable admission first."""
    root = cfg.state_root / "tickets"
    marks = sorted(root.glob("*/PARKED"), key=_park_order) if root.exists() else []
    return [m.parent.name for m in marks if (m.parent / "state.json").exists()]


def park(cfg, key: str) -> None:
    """Hold a ticket back without losing its state: stop its supervisor; the drip resumes it later."""
    import supervise
    tdir = cfg.ticket_dir(key)
    sequence = 10**12
    if cohort_enabled(cfg):
        slot = next((row for row in cohort.load(cfg).slots if row.ticket == key), None)
        if slot:
            sequence = slot.admission_sequence
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "PARKED.json").write_text(json.dumps({"admission_sequence": sequence}, indent=2))
    (tdir / "PARKED").touch()
    if cohort_enabled(cfg):
        cohort.park_member(cfg, key)          # after the marker is durable, so a crash cannot lose the ticket
    supervise.stop(key)


def in_flight(cfg) -> list[str]:
    if cohort_enabled(cfg):
        return cohort.members(cfg)
    out = []
    tdir_root = cfg.state_root / "tickets"
    if not tdir_root.exists():
        return out
    for td in tdir_root.iterdir():
        if not (td / "state.json").exists():
            continue
        t = state.load(td)
        if (td / "PARKED").exists() or state.human_owned(td):
            continue                              # held back by the 4-at-a-time throttle; resumed first when a slot frees
        if t.state in IN_FLIGHT_STATES and pathlib.Path(t.worktree or WORKTREES / t.key.lower()).is_dir():
            out.append(t.key)
    return sorted(out)


def candidates(cfg, log=print) -> list[dict]:
    """Board order minus exclusions, in-flight, gated, dep-blocked, and (optionally) others' tickets."""
    rows = board(cfg)
    flight = set(in_flight(cfg))
    gated = set()
    for td in (cfg.state_root / "tickets").glob("*/state.json"):
        t = state.load(td.parent)
        if t.state in TERMINAL:
            gated.add(t.key)
    all_deps = sorted({d for spec in cfg.tickets.values() for d in spec.deps})
    done = done_in_jira(all_deps)
    out = []
    for r in rows:
        spec = cfg.tickets.get(r["key"])
        if spec and spec.exclude:
            log(f"  skip {r['key']}: excluded ({spec.exclude})")
            continue
        if r["key"] in flight or r["key"] in gated or state.human_owned(cfg.ticket_dir(r["key"])):
            continue
        if spec and any(d not in done for d in spec.deps):
            log(f"  skip {r['key']}: deps not Done in Jira: {[d for d in spec.deps if d not in done]}")
            continue
        if r["status"] not in READY_STATUSES:
            # Only the board's "ready" column is fair game: In Progress means a person is working it
            # (Martin's adoption tickets); Triage/Backlog means it is not yet groomed.
            log(f"  skip {r['key']}: status {r['status']!r} is not a ready column")
            continue
        if cfg.drip.get("skip_assigned_to_others") and r["assignee"] and r["assignee"] != ME:
            log(f"  skip {r['key']}: assigned to {r['assignee']}")
            continue
        out.append(r)
    return out


MAX_SPINUP_ATTEMPTS = 3
SPINUP_BACKOFF_S = 5 * 60
SPINUP_BACKOFF_MAX_S = 6 * 3600


def _spinup_waiting(cfg, key: str) -> bool:
    try:
        return json.loads((cfg.ticket_dir(key) / "spinup-backoff.json").read_text())["next_epoch"] > time.time()
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False
CHAIN_OK = ("ok", "serving-timeout")


class SpinupFailed(RuntimeError):
    def __init__(self, status: str, worktree: str | None):
        super().__init__(f"{status} in {worktree}")
        self.status, self.worktree = status, worktree


def _spinup_modules(cfg):
    return workspaces.load_helper(cfg, "spinup_helper")


def _claim_in_jira(sh, ticket: dict, log) -> None:
    """Only after setup succeeded: a failed setup must leave the ticket where the drip can see it."""
    key = ticket["key"]
    try:
        sh.transition_to_in_progress(key, ticket["status"])
    except Exception as e:  # noqa: BLE001
        log(f"{key}: Jira transition failed (continuing): {e}")
    subprocess.run(["acli", "jira", "workitem", "assign", "--key", key, "--assignee", ME], capture_output=True, timeout=60)
    try:
        sh.mark_spunup(key)
    except Exception as exc:  # noqa: BLE001
        log(f"{key}: spinup marker unavailable (continuing): {type(exc).__name__}")


def _workspace_action(cfg, key, action, log=print):
    try:
        result = action()
    except (workspaces.WorkspaceError, workspace_binding.BindingError) as exc:
        t = state.load(cfg.ticket_dir(key))
        try:
            binding = workspace_binding.read(cfg.ticket_dir(key))
        except workspace_binding.BindingError:
            binding = None
        status = exc.status if isinstance(exc, workspaces.WorkspaceError) else "workspace-binding-invalid"
        raise SpinupFailed(status, binding.worktree if binding else t.worktree) from exc
    if result.status not in CHAIN_OK:
        raise SpinupFailed(result.status, result.binding.worktree)
    for warning in result.warnings:
        log(f"{key}: {warning}")
    return pathlib.Path(result.binding.worktree)


def spinup_with_panes(cfg, key: str, log=print) -> pathlib.Path:
    """Create all workspace views using the shared lifecycle and configured backend."""
    sh = _spinup_modules(cfg)
    ticket = sh.fetch_ticket(key)
    branch = sh.derive_branch(ticket["key"], ticket["issue_type"], ticket["title"])
    wt = _workspace_action(cfg, key, lambda: workspaces.launch(cfg, key, branch, f"/loop-watch {key}",
                                                              f"https://example.atlassian.net/browse/{key}"), log=log)
    log(f"{key}: spinup completed worktree={wt}")
    _claim_in_jira(sh, ticket, log)
    return wt


def retry_spinup(cfg, t: state.Ticket, log=print) -> pathlib.Path:
    """Retry in place using the recorded backend, never the new-launch default."""
    wt = _workspace_action(cfg, t.key, lambda: workspaces.retry_setup(cfg, t, f"/loop-watch {t.key}",
                                                                    f"https://example.atlassian.net/browse/{t.key}"), log=log)
    sh = _spinup_modules(cfg)
    _claim_in_jira(sh, sh.fetch_ticket(t.key), log)
    return wt


def spinup_retries(cfg) -> list[state.Ticket]:
    out = []
    root = cfg.state_root / "tickets"
    for tdir in sorted(root.iterdir()) if root.exists() else []:
        if not (tdir / "state.json").exists() or state.human_owned(tdir):
            continue
        t = state.load(tdir)
        if (t.state == "blocked" and t.reason.startswith("spinup failed")
                and t.spinup_failures < MAX_SPINUP_ATTEMPTS and pathlib.Path(t.worktree).is_dir()):
            out.append(t)
    return out


def record_spinup_failure(cfg, key: str, e: SpinupFailed, log=print) -> None:
    """Record setup failure without releasing a cohort reservation."""
    tdir = cfg.ticket_dir(key)
    t = state.load(tdir) if (tdir / "state.json").exists() else state.Ticket(key=key)
    n = t.spinup_failures = t.spinup_failures + 1
    t.state, t.worktree = "blocked", e.worktree or t.worktree
    if cohort_enabled(cfg):
        nxt = "cohort retains the slot and retries this ticket"
    else:
        nxt = "needs operator" if n >= MAX_SPINUP_ATTEMPTS else "the drip re-runs setup next pass"
    t.reason = f"spinup failed ({e.status}), attempt {n}/{MAX_SPINUP_ATTEMPTS}; {nxt}"
    state.save(tdir, t)
    if cohort_enabled(cfg):
        # Keep the reservation, but back off: each retry can open a cmux workspace and runs setup.
        delay = min(SPINUP_BACKOFF_S * 2 ** (n - 1), SPINUP_BACKOFF_MAX_S)
        (tdir / "spinup-backoff.json").write_text(json.dumps({"next_epoch": time.time() + delay, "failures": n}))
        if n == MAX_SPINUP_ATTEMPTS:
            import lifecycle
            lifecycle._notify_once(tdir, key, f"{key} spinup keeps failing", f"{e.status}; slot kept, retrying every {delay // 60} min")
    if cohort_enabled(cfg) and not state.load(tdir).recovery.get("active"):
        contract = recovery.new_contract(
            key,
            {"state": "spinup", "previous": t.previous, "head_sha": t.head_sha},
            {"kind": "retry-spinup", "state": "spinup"},
        )
        recovery.activate(cfg, key, contract)
    log(f"drip: {key} {t.reason}")


def shared_services_ready(log=print) -> bool:
    """Postgres, Redis and Elasticsearch must answer before a spinup; Elasticsearch needs ~35s after login."""
    try:
        r = subprocess.run([str(example_APP / "bin" / "dev-services"), "status"], cwd=example_APP,
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"drip: shared services check failed: {e}")
        return False
    if r.returncode != 0:
        log(f"drip: {(r.stdout + r.stderr).strip()}")
        return False
    return True


def tick(cfg, log=print, dry_run: bool = False) -> list[str]:
    """One drip tick: fill free slots from the top of the board. Returns keys started.

    Serialized by a lease: selection reads in-flight state, then mutates Jira and creates a worktree.
    A scheduled tick racing `/agent-loop start` could select the same ticket twice and build two
    workspaces and two supervisors for it (branch review 2026-09-23)."""
    import locks
    if cohort_enabled(cfg):
        report = cohort.reconcile(cfg, dry_run=dry_run)
        for error in report.errors:
            log(f"cohort: {error}")
        return report.admitted
    lease = locks.Lease(pathlib.Path(cfg.state_root) / "locks" / "drip", "drip")
    if not lease.acquire(hold=True):
        log("drip: another drip tick holds the lease; skipping")
        return []
    try:
        return _tick_locked(cfg, log, dry_run)
    finally:
        lease.release()


def _finish_spinup(cfg, key: str, wt, supervise) -> None:
    tdir = cfg.ticket_dir(key)
    t = state.load(tdir) if (tdir / "state.json").exists() else state.Ticket(key=key)
    t.state, t.reason = ("queued", "") if t.state == "blocked" else (t.state, t.reason)
    t.worktree = str(wt)
    t.branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
    if cohort_enabled(cfg) and isinstance(t.recovery, dict):
        active = t.recovery.get("active")
        if isinstance(active, dict) and active.get("resume", {}).get("kind") == "retry-spinup":
            t.recovery["active"] = None
    state.save(tdir, t)
    if cohort_enabled(cfg):
        supervise.ensure_running(key, str(wt), cfg.state_root)
    else:
        supervise.start(key, str(wt), cfg.state_root)
    if cohort_enabled(cfg):
        cohort.activate(cfg, key)


def _tick_locked(cfg, log=print, dry_run: bool = False, supervisor=None) -> list[str]:
    import supervise as default_supervisor
    supervise = supervisor or default_supervisor
    enabled = cohort_enabled(cfg)
    cap = limit(cfg)
    flight = in_flight(cfg)
    free = cap - len(flight)
    noun = "cohort" if enabled else "in flight"
    log(f"drip: {noun} {flight} ({len(flight)}/{cap})")
    reserved = [row for row in cohort.load(cfg).slots if row.ticket and row.phase == "reserved"] if enabled else []
    if enabled and free <= 0 and not reserved:
        return []
    if not dry_run and not shared_services_ready(log):
        log("drip: shared services are not ready; skipping this tick")
        return []

    started = []
    if enabled:
        for slot in reserved:
            key = slot.ticket
            if key is None:
                continue
            if state.human_owned(cfg.ticket_dir(key)) or _spinup_waiting(cfg, key):
                continue
            log(f"drip: retrying reserved {key}")
            if dry_run:
                started.append(key)
                continue
            t = state.load(cfg.ticket_dir(key))
            if t.worktree and pathlib.Path(t.worktree).is_dir() and (slot.source == "parked" or (t.branch and t.state not in ("blocked", "paused"))):
                (cfg.ticket_dir(key) / "PARKED").unlink(missing_ok=True)
                (cfg.ticket_dir(key) / "PARKED.json").unlink(missing_ok=True)
                supervise.ensure_running(key, t.worktree, cfg.state_root)
                cohort.activate(cfg, key)
                started.append(key)
                continue
            spin = (lambda t=t: retry_spinup(cfg, t, log)) if t.worktree and pathlib.Path(t.worktree).is_dir() else (lambda key=key: spinup_with_panes(cfg, key, log))
            try:
                wt = spin()
            except SpinupFailed as e:
                record_spinup_failure(cfg, key, e, log)
                continue
            except Exception as e:  # noqa: BLE001
                log(f"drip: {key} reserved spinup failed: {e}")
                continue
            _finish_spinup(cfg, key, wt, supervise)
            started.append(key)
        flight = in_flight(cfg)
        free = cap - len(flight)

    if free <= 0:
        return started

    import workspace_capacity
    workspace_plan = 0

    def reserve_new(key, source):
        nonlocal workspace_plan
        try:
            if enabled and not dry_run:
                cohort.reserve(cfg, key, source)
            elif cfg.workspace_limit is not None:
                inventory = workspace_capacity.observe(cfg)
                value = cohort.load(cfg) if enabled else None
                workspace_plan += workspace_capacity.check(
                    cfg, inventory, key=key, cohort=value, extra=workspace_plan if dry_run else 0)
        except (cohort.WorkspaceCapacityError, workspace_capacity.CapacityError) as exc:
            log(f"drip: {exc}")
            return False
        return True

    occupied = set(flight)
    parked_started = 0
    for key in [candidate for candidate in parked(cfg) if candidate not in occupied and not state.human_owned(cfg.ticket_dir(candidate))][:free]:
        if not reserve_new(key, "parked"):
            return started
        log(f"drip: resuming parked {key}")
        if not dry_run:
            (cfg.ticket_dir(key) / "PARKED").unlink(missing_ok=True)
            (cfg.ticket_dir(key) / "PARKED.json").unlink(missing_ok=True)
            if enabled:
                supervise.ensure_running(key, state.load(cfg.ticket_dir(key)).worktree, cfg.state_root)
            else:
                supervise.start(key, state.load(cfg.ticket_dir(key)).worktree, cfg.state_root)
            if enabled:
                cohort.activate(cfg, key)
        started.append(key)
        parked_started += 1
    free -= parked_started
    if free <= 0:
        return started

    work = [] if enabled else [(t.key, lambda t=t: retry_spinup(cfg, t, log), "retry setup") for t in spinup_retries(cfg)]
    work += [(r["key"], lambda r=r: spinup_with_panes(cfg, r["key"], log), r["summary"][:60]) for r in candidates(cfg, log)]
    for key, spin, what in work[:free]:
        if not reserve_new(key, "drip"):
            return started
        log(f"drip: next → {key} {what}")
        if dry_run:
            started.append(key)
            continue
        try:
            wt = spin()
        except SpinupFailed as e:
            if enabled or (e.worktree and pathlib.Path(e.worktree).is_dir()):
                record_spinup_failure(cfg, key, e, log)
            else:
                log(f"drip: {key} spinup failed before a worktree existed: {e}")
            log("drip: stopping this pass after a spinup failure")
            break
        except Exception as e:  # noqa: BLE001
            log(f"drip: {key} spinup failed: {e}")
            break
        _finish_spinup(cfg, key, wt, supervise)
        started.append(key)
    return started
