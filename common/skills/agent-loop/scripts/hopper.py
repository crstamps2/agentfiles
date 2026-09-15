"""run-hopper: drive several tickets from the hopper to Human Gate 1, unattended.

Order = hopper.toml order. A ticket is eligible when every `deps` ticket is `done` (merged): the
pilot does not stack PRs autonomously. Each ticket gets the normal spinup (worktree + setup via
the cmux chain, headless: no agent tab) and then the lifecycle. Tickets are round-robined: while
one waits on CI or the review bots, the next one plans/implements. The heavy lane still serialises
implementation. A ticket that reaches Human Gate 1 has its dev server stopped to free memory for
the next; the morning report says how to start it again.

The wake lease (`caffeinate -i`) is held only while this process runs.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

import lifecycle
import state

TERMINAL_FOR_TONIGHT = ("human-gate-1", "done")
STOPPED = TERMINAL_FOR_TONIGHT + ("blocked",)
SPINUP_SCRIPTS = pathlib.Path("~/.claude/skills/spinup/scripts").expanduser()
WORKTREES = pathlib.Path("~/workspace/zipline-worktrees").expanduser()


def eligible(cfg, key: str) -> tuple[bool, str]:
    spec = cfg.tickets[key]
    for d in spec.deps:
        ds = state.load(cfg.ticket_dir(d)).state if (cfg.ticket_dir(d) / "state.json").exists() else "queued"
        if ds != "done":
            return False, f"dep {d} is {ds}"
    return True, ""


def worktree_for(key: str) -> pathlib.Path | None:
    p = WORKTREES / key.lower()
    return p if (p / ".git").exists() or (p / "bin").exists() else None


def spinup(key: str, log) -> pathlib.Path:
    """Cody's spinup chain, headless. Blocks through bin/worktree-setup (~10 min)."""
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
    res = cc.run_chain(name=name, branch=branch, new_branch=True, prompt="", ref_url=None,
                       group=getattr(cc, "GROUP_IN_PROGRESS", None), harness=None)
    log(f"{key}: spinup {res.get('status')} worktree={res.get('worktree')} workspace={res.get('workspace')}")
    if res.get("status") not in ("ok", "serving-timeout"):
        raise RuntimeError(f"spinup failed: {res}")
    try:
        sh.mark_spunup(ticket["key"])
    except Exception:  # noqa: BLE001
        pass
    wt = pathlib.Path(res["worktree"])
    (WORKTREES / ".metadata_never_index").touch()
    return wt


def stop_dev_server(wt: pathlib.Path, log) -> None:
    r = subprocess.run(["bash", "-lc", "bin/wt dev --stop"], cwd=str(wt), capture_output=True, text=True, errors="replace", timeout=300)
    log(f"{wt.name}: dev server stopped rc={r.returncode}")


def run_hopper(cfg, runner, ctx, *, max_new_tickets: int, max_hours: float, log=print) -> dict:
    deadline = time.monotonic() + max_hours * 3600
    started_tonight: list[str] = []
    report = {"tickets": {}}
    caff = subprocess.Popen(["caffeinate", "-i"])
    try:
        while time.monotonic() < deadline:
            if state.paused(cfg.state_root):
                log("PAUSE present; stopping"); break
            progressed = False; all_stopped = True
            for i, key in enumerate(cfg.tickets, 1):
                tdir = cfg.ticket_dir(key); t = state.load(tdir) if (tdir / "state.json").exists() else None
                st = t.state if t else "queued"
                if st in STOPPED:
                    continue
                ok, why = eligible(cfg, key)
                if not ok:
                    continue
                wt = worktree_for(key)
                if wt is None:
                    if key not in started_tonight and len(started_tonight) >= max_new_tickets:
                        continue
                    if key not in started_tonight:
                        started_tonight.append(key)
                    log(f"{key}: spinning up")
                    try:
                        wt = spinup(key, log)
                    except Exception as e:  # noqa: BLE001
                        log(f"{key}: spinup failed: {e}"); report["tickets"][key] = {"state": "spinup-failed", "error": str(e)}
                        tt = state.load(tdir); state.save(tdir, state.transition(tt, "paused", reason=f"spinup failed: {e}")); continue
                    tt = state.load(tdir); tt.worktree = str(wt); tt.branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"], capture_output=True, text=True).stdout.strip(); state.save(tdir, tt)
                elif key not in started_tonight and st == "queued":
                    if len(started_tonight) >= max_new_tickets:
                        continue
                    started_tonight.append(key)
                all_stopped = False
                steps = lifecycle.run_once(cfg, runner, ctx, key, wt, hopper_index=i, max_steps=40)
                last = steps[-1] if steps else None
                t = state.load(tdir)
                report["tickets"][key] = {"state": t.state, "reason": t.reason, "last": repr(last), "worktree": str(wt)}
                if t.state in TERMINAL_FOR_TONIGHT:
                    stop_dev_server(wt, log); progressed = True
                elif last is not None and not last.wait:
                    progressed = True
                elif last is not None and last.wait and last.stage == "implement" and ("resource:" in (last.detail or "") or "heavy lane" in (last.detail or "")):
                    progressed = True     # transient; loop again after the sleep below
            if all_stopped:
                log("every eligible ticket is at Human Gate 1 / stopped"); break
            if not progressed:
                for _ in range(18):        # 3 min in 10 s slices; PAUSE exits
                    if state.paused(cfg.state_root): break
                    time.sleep(10)
    finally:
        caff.terminate()
    report["started_tonight"] = started_tonight
    return report
