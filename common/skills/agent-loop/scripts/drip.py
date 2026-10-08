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
import os
import pathlib
import subprocess
import sys

import state

IN_FLIGHT_STATES = {"spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready", "bot-loop", "paused"}
TERMINAL = {"human-gate-1", "done", "blocked", "colleague-loop", "human-gate-2"}
WORKTREES = pathlib.Path("~/workspace/zipline-worktrees").expanduser()
SPINUP_SCRIPTS = pathlib.Path("~/.claude/skills/spinup/scripts").expanduser()
ZIPLINE_APP = pathlib.Path("~/workspace/zipline-app").expanduser()
ME = "cody@zipline.inc"
HARD_MAX_IN_FLIGHT = 7             # Cody, 2026-10-08: never more than 7 tickets processing


def limit(cfg) -> int:
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


def parked(cfg) -> list[str]:
    """Parked tickets, oldest first."""
    root = cfg.state_root / "tickets"
    marks = sorted(root.glob("*/PARKED"), key=lambda p: p.stat().st_mtime) if root.exists() else []
    return [m.parent.name for m in marks if (m.parent / "state.json").exists()]


def park(cfg, key: str) -> None:
    """Hold a ticket back without losing its state: stop its supervisor; the drip resumes it later."""
    import supervise
    (cfg.ticket_dir(key) / "PARKED").touch()
    supervise.stop(key)


def in_flight(cfg) -> list[str]:
    out = []
    tdir_root = cfg.state_root / "tickets"
    if not tdir_root.exists():
        return out
    for td in tdir_root.iterdir():
        if not (td / "state.json").exists():
            continue
        t = state.load(td)
        if (td / "PARKED").exists():
            continue                              # held back by the 4-at-a-time throttle; resumed first when a slot frees
        if t.state in IN_FLIGHT_STATES and (WORKTREES / t.key.lower()).exists():
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
            log(f"  skip {r['key']}: excluded ({spec.exclude})"); continue
        if r["key"] in flight or r["key"] in gated:
            continue
        if spec and any(d not in done for d in spec.deps):
            log(f"  skip {r['key']}: deps not Done in Jira: {[d for d in spec.deps if d not in done]}"); continue
        if r["status"] not in READY_STATUSES:
            # Only the board's "ready" column is fair game: In Progress means a person is working it
            # (Martin's adoption tickets); Triage/Backlog means it is not yet groomed.
            log(f"  skip {r['key']}: status {r['status']!r} is not a ready column"); continue
        if cfg.drip.get("skip_assigned_to_others") and r["assignee"] and r["assignee"] != ME:
            log(f"  skip {r['key']}: assigned to {r['assignee']}"); continue
        out.append(r)
    return out


MAX_SPINUP_ATTEMPTS = 3
CHAIN_OK = ("ok", "serving-timeout")


class SpinupFailed(RuntimeError):
    def __init__(self, status: str, worktree: str | None):
        super().__init__(f"{status} in {worktree}")
        self.status, self.worktree = status, worktree


def _spinup_modules():
    sys.path.insert(0, str(SPINUP_SCRIPTS))
    import cmux_chain, spinup_helper  # noqa: E402
    return cmux_chain, spinup_helper


def _chain_kwargs(cc, key: str) -> dict:
    return {"prompt": f"/loop-watch {key}", "ref_url": f"{cc.JIRA_BROWSE_BASE}/{key}",
            "group": getattr(cc, "GROUP_IN_PROGRESS", None), "harness": "pi"}


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
    except Exception:  # noqa: BLE001
        pass


def spinup_with_panes(key: str, log=print) -> pathlib.Path:
    """Cody's cmux chain with ALL panes: setup, dev server, browser, Jira; the agent tab runs /loop-watch."""
    cc, sh = _spinup_modules()
    ticket = sh.fetch_ticket(key)
    branch = sh.derive_branch(ticket["key"], ticket["issue_type"], ticket["title"])
    name = cc.worktree_name_for_ticket(ticket["key"])
    res = cc.run_chain(name=name, branch=branch, new_branch=True, **_chain_kwargs(cc, key))
    log(f"{key}: spinup {res.get('status')} worktree={res.get('worktree')} workspace={res.get('workspace')}")
    if res.get("status") not in CHAIN_OK:
        raise SpinupFailed(res.get("status"), res.get("worktree"))
    _claim_in_jira(sh, ticket, log)
    (WORKTREES / ".metadata_never_index").touch()
    return pathlib.Path(res["worktree"])


def rerun_setup(worktree: str, name: str) -> tuple[bool, str]:
    """Re-run bin/worktree-setup in an existing worktree, the way the setup tab runs it."""
    cc, _ = _spinup_modules()
    env = dict(os.environ, WORKTREE_NAME=name, WORKTREE_ROOT_PATH=str(cc.MAIN_REPO))
    try:
        r = subprocess.run(["bin/worktree-setup"], cwd=worktree, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
    except subprocess.TimeoutExpired:
        return False, "bin/worktree-setup timed out after 30 minutes"
    return r.returncode == 0, (r.stdout + r.stderr)[-20000:]


def retry_spinup(cfg, t: state.Ticket, log=print) -> pathlib.Path:
    """Complete a failed spinup in place: same worktree and workspace, no duplicate."""
    cc, sh = _spinup_modules()
    name = cc.worktree_name_for_ticket(t.key)
    workspace = cc.workspace_ref_for_name(name)
    if not workspace:
        raise SpinupFailed("workspace-closed", t.worktree)
    ok, output = rerun_setup(t.worktree, name)
    (cfg.ticket_dir(t.key) / "spinup-setup.log").write_text(output)
    if not ok:
        raise SpinupFailed("setup-failed", t.worktree)
    (pathlib.Path(t.worktree) / ".cmux-setup-status").write_text("EXIT:0\n")
    res = cc.finish_chain(name, workspace, t.worktree, **_chain_kwargs(cc, t.key))
    log(f"{t.key}: spinup retry {res.get('status')} workspace={workspace}")
    if res.get("status") not in CHAIN_OK:
        raise SpinupFailed(res.get("status"), t.worktree)
    _claim_in_jira(sh, sh.fetch_ticket(t.key), log)
    return pathlib.Path(t.worktree)


def spinup_retries(cfg) -> list[state.Ticket]:
    out = []
    root = cfg.state_root / "tickets"
    for tdir in sorted(root.iterdir()) if root.exists() else []:
        if not (tdir / "state.json").exists() or (tdir / "HUMAN").exists():
            continue
        t = state.load(tdir)
        if (t.state == "blocked" and t.reason.startswith("spinup failed")
                and t.spinup_failures < MAX_SPINUP_ATTEMPTS and pathlib.Path(t.worktree).is_dir()):
            out.append(t)
    return out


def record_spinup_failure(cfg, key: str, e: SpinupFailed, log=print) -> None:
    """Blocked keeps the drip from re-running the chain (a duplicate workspace) and frees the slot."""
    tdir = cfg.ticket_dir(key)
    t = state.load(tdir) if (tdir / "state.json").exists() else state.Ticket(key=key)
    n = t.spinup_failures = t.spinup_failures + 1
    t.state, t.worktree = "blocked", e.worktree or t.worktree
    nxt = "needs operator" if n >= MAX_SPINUP_ATTEMPTS else "the drip re-runs setup next pass"
    t.reason = f"spinup failed ({e.status}), attempt {n}/{MAX_SPINUP_ATTEMPTS}; {nxt}"
    state.save(tdir, t)
    log(f"drip: {key} {t.reason}")


def shared_services_ready(log=print) -> bool:
    """Postgres, Redis and Elasticsearch must answer before a spinup; Elasticsearch needs ~35s after login."""
    try:
        r = subprocess.run([str(ZIPLINE_APP / "bin" / "dev-services"), "status"], cwd=ZIPLINE_APP,
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
    import locks, supervise
    lease = locks.Lease(pathlib.Path(cfg.state_root) / "locks" / "drip", "drip")
    if not lease.acquire(hold=True):
        log("drip: another drip tick holds the lease; skipping")
        return []
    try:
        return _tick_locked(cfg, log, dry_run)
    finally:
        lease.release()


def _tick_locked(cfg, log=print, dry_run: bool = False) -> list[str]:
    import supervise
    cap = limit(cfg)
    flight = in_flight(cfg)
    free = cap - len(flight)
    log(f"drip: in flight {flight} ({len(flight)}/{cap})")
    if free <= 0:
        return []
    if not dry_run and not shared_services_ready(log):
        log("drip: shared services are not ready; skipping this tick")
        return []
    started = []
    for key in parked(cfg)[:free]:
        log(f"drip: resuming parked {key}")
        if not dry_run:
            (cfg.ticket_dir(key) / "PARKED").unlink(missing_ok=True)
            supervise.start(key, state.load(cfg.ticket_dir(key)).worktree, cfg.state_root)
        started.append(key)
    free -= len(started)
    if free <= 0:
        return started
    work = [(t.key, lambda t=t: retry_spinup(cfg, t, log), "retry setup") for t in spinup_retries(cfg)]
    work += [(r["key"], lambda r=r: spinup_with_panes(r["key"], log), r["summary"][:60]) for r in candidates(cfg, log)]
    for key, spin, what in work[:free]:
        log(f"drip: next → {key} {what}")
        if dry_run:
            started.append(key); continue
        try:
            wt = spin()
        except SpinupFailed as e:
            if e.worktree and pathlib.Path(e.worktree).is_dir():
                record_spinup_failure(cfg, key, e, log)
            else:
                log(f"drip: {key} spinup failed before a worktree existed: {e}")
            log("drip: stopping this pass after a spinup failure"); break
        except Exception as e:  # noqa: BLE001
            log(f"drip: {key} spinup failed: {e}"); break
        tdir = cfg.ticket_dir(key); t = state.load(tdir) if (tdir / "state.json").exists() else state.Ticket(key=key)
        t.state, t.reason = ("queued", "") if t.state == "blocked" else (t.state, t.reason)
        t.worktree = str(wt); t.branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
        state.save(tdir, t)
        supervise.start(key, str(wt), cfg.state_root)
        started.append(key)
    return started
