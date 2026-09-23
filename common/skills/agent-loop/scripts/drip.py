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
import sys

import state

IN_FLIGHT_STATES = {"spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready", "bot-loop", "paused"}
TERMINAL = {"human-gate-1", "done", "blocked", "colleague-loop", "human-gate-2"}
WORKTREES = pathlib.Path("~/workspace/zipline-worktrees").expanduser()
SPINUP_SCRIPTS = pathlib.Path("~/.claude/skills/spinup/scripts").expanduser()
ME = "cody@zipline.inc"
READY_STATUSES = {"Selected for Work", "To Do", "Ready", "Ready for Development"}


def _acli_json(args: list[str]):
    r = subprocess.run(["acli", *args, "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode or not r.stdout.strip():
        raise RuntimeError(f"acli {' '.join(args[:3])} failed: {r.stderr.strip()[:200]}")
    d = json.loads(r.stdout)
    return d if isinstance(d, list) else d.get("issues", d.get("values", []))


def board(cfg) -> list[dict]:
    """Epic children not Done, in board Rank order."""
    d = cfg.drip; epic = d.get("epic", cfg.epic)
    types = d.get("issue_types") or []
    jql = f"parent = {epic} AND statusCategory != Done"
    if types:
        jql += " AND issuetype in (" + ",".join(f'"{t}"' for t in types) + ")"
    if d.get("jql_extra"):
        jql += f" AND ({d['jql_extra']})"
    jql += " ORDER BY Rank ASC"
    rows = _acli_json(["jira", "workitem", "search", "--jql", jql, "--fields", "key,summary,status,assignee,issuetype"])
    return [{"key": r["key"], "summary": r["fields"]["summary"], "status": r["fields"]["status"]["name"],
             "assignee": ((r["fields"].get("assignee") or {}).get("emailAddress") or (r["fields"].get("assignee") or {}).get("displayName") or None)} for r in rows]


def done_in_jira(keys: list[str]) -> set[str]:
    if not keys:
        return set()
    rows = _acli_json(["jira", "workitem", "search", "--jql", f"key in ({','.join(keys)}) AND statusCategory = Done", "--fields", "key"])
    return {r["key"] for r in rows}


def in_flight(cfg) -> list[str]:
    out = []
    tdir_root = cfg.state_root / "tickets"
    if not tdir_root.exists():
        return out
    for td in tdir_root.iterdir():
        if not (td / "state.json").exists():
            continue
        t = state.load(td)
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


def spinup_with_panes(key: str, log=print) -> pathlib.Path:
    """Cody's cmux chain with ALL panes: setup, dev server, browser, Jira; the agent tab runs /loop-watch."""
    sys.path.insert(0, str(SPINUP_SCRIPTS))
    import cmux_chain as cc  # noqa: E402
    import spinup_helper as sh  # noqa: E402
    ticket = sh.fetch_ticket(key)
    branch = sh.derive_branch(ticket["key"], ticket["issue_type"], ticket["title"])
    name = cc.worktree_name_for_ticket(ticket["key"])
    try:
        sh.transition_to_in_progress(ticket["key"], ticket["status"])
    except Exception as e:  # noqa: BLE001
        log(f"{key}: Jira transition failed (continuing): {e}")
    subprocess.run(["acli", "jira", "workitem", "assign", "--key", key, "--assignee", ME], capture_output=True, timeout=60)
    res = cc.run_chain(name=name, branch=branch, new_branch=True, prompt=f"/loop-watch {key}", ref_url=f"{cc.JIRA_BROWSE_BASE}/{key}",
                       group=getattr(cc, "GROUP_IN_PROGRESS", None), harness="pi")
    log(f"{key}: spinup {res.get('status')} worktree={res.get('worktree')} workspace={res.get('workspace')}")
    if res.get("status") not in ("ok", "serving-timeout"):
        raise RuntimeError(f"spinup failed: {res}")
    try:
        sh.mark_spunup(ticket["key"])
    except Exception:  # noqa: BLE001
        pass
    (WORKTREES / ".metadata_never_index").touch()
    return pathlib.Path(res["worktree"])


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
    limit = int(cfg.drip.get("max_in_flight", 3))
    flight = in_flight(cfg)
    free = limit - len(flight)
    log(f"drip: in flight {flight} ({len(flight)}/{limit})")
    if free <= 0:
        return []
    started = []
    for r in candidates(cfg, log)[:free]:
        log(f"drip: next from board → {r['key']} {r['summary'][:60]}")
        if dry_run:
            started.append(r["key"]); continue
        try:
            wt = spinup_with_panes(r["key"], log)
        except Exception as e:  # noqa: BLE001
            log(f"drip: {r['key']} spinup failed: {e}"); continue
        tdir = cfg.ticket_dir(r["key"]); t = state.load(tdir) if (tdir / "state.json").exists() else state.Ticket(key=r["key"])
        t.worktree = str(wt); t.branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
        state.save(tdir, t)
        supervise.start(r["key"], str(wt), cfg.state_root)
        started.append(r["key"])
    return started
