"""Foreground ownership is independent of autonomous cohort admission."""
from __future__ import annotations

import json
import os
import pathlib

import cohort
import locks
import state

MARKER = "INTERACTIVE.json"


def owned(tdir) -> bool:
    return (pathlib.Path(tdir) / MARKER).exists()


def _read(tdir):
    value = json.loads((tdir / MARKER).read_text())
    if not isinstance(value, dict) or value.get("mode") not in ("yielding", "interactive"):
        raise ValueError("invalid interactive ownership record")
    return value


def _write(tdir, value):
    target = tdir / MARKER
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2))
    os.replace(tmp, target)


def _ticket(cfg, worktree):
    cwd = pathlib.Path(worktree).resolve()
    matches = []
    for path in (cfg.state_root / "tickets").glob("*/state.json"):
        ticket = state.load(path.parent)
        if ticket.worktree:
            root = pathlib.Path(ticket.worktree).resolve()
            if cwd == root or root in cwd.parents:
                matches.append((len(root.parts), ticket.key))
    return max(matches)[1] if matches else None


def _settle_locked(cfg, key, supervisor):
    """Called under the cohort lease; take the SAME ticket lease as every writer before handoff."""
    import reconcile
    tdir = cfg.ticket_dir(key)
    record = _read(tdir)
    if record["mode"] != "interactive":
        if (cfg.state_root / "cohort.json").exists():
            slot = cohort._find(cohort.load(cfg), key)
            if slot and slot.phase == "reserved" and locks.is_held(cfg.state_root / "locks/drip"):
                return record  # Spinup runs outside the runner lease, but owns the admission lease.
        lease = locks.Lease(cfg.state_root / "locks" / f"runner-{key.lower()}", "interactive-handoff")
        if not lease.acquire(hold=True):
            return record
        try:
            # A legacy HUMAN takeover may already contain manual edits; never restore over those.
            if (tdir / "HUMAN").exists():
                import attempt
                import procid
                for leaf in reconcile._leaf_dirs(cfg.state_root / "attempts" / key):
                    rec = attempt.load(leaf)
                    if procid.classify(procid.ProcId.from_dict(rec.proc) if rec.proc else None) != "dead":
                        raise ValueError(f"{key} has an unresolved worker under legacy HUMAN ownership; manual edits preserved")
            else:
                reconcile._sweep(cfg, key, include_interactive=True)
            supervisor.stop(key)
            record["mode"] = "interactive"
            _write(tdir, record)
        finally:
            lease.release()
    if (cfg.state_root / "cohort.json").exists():
        value = cohort.load(cfg)
        slot = cohort._find(value, key)
        if slot:
            value.history.append({"event": "interactive", "ticket": key, "slot": slot.slot,
                                  "admission_sequence": slot.admission_sequence, "utc": cohort._now()})
            value.slots[slot.slot - 1] = cohort.Slot(slot=slot.slot)
            value.generation += 1
            cohort._save_unlocked(cfg, value)
    return record


def status(cfg, worktree):
    key = _ticket(cfg, worktree)
    if not key:
        return {"mode": "unmanaged", "ticket": None}
    tdir = cfg.ticket_dir(key)
    return _read(tdir) if owned(tdir) else {"mode": "autonomous", "ticket": key}


def enter(cfg, worktree, *, supervisor=None, session="", claim=True):
    import supervise
    supervisor = supervisor or supervise
    key = _ticket(cfg, worktree)
    if not key:
        return {"mode": "unmanaged", "ticket": None}
    with cohort._lease(cfg) as acquired:
        if not acquired:
            raise ValueError("ownership controller busy; retry the handoff")
        tdir = cfg.ticket_dir(key)
        if not owned(tdir) and not claim:
            return {"mode": "autonomous", "ticket": key}
        if owned(tdir):
            record = _read(tdir)
            if not claim and record.get("session", "") != session:
                return {"mode": "other-session", "ticket": key}
            if claim and record.get("session", "") != session:
                record["session"] = session
                _write(tdir, record)
        else:
            sequence = 10**12
            if (tdir / "PARKED.json").exists():
                sequence = json.loads((tdir / "PARKED.json").read_text())["admission_sequence"]
            if (cfg.state_root / "cohort.json").exists():
                slot = cohort._find(cohort.load(cfg), key)
                if slot:
                    sequence = slot.admission_sequence
            _write(tdir, {"mode": "yielding", "ticket": key, "admission_sequence": sequence,
                          "since": cohort._now(), "session": session})
        return _settle_locked(cfg, key, supervisor)


def return_to_loop(cfg, worktree, *, session=None):
    key = _ticket(cfg, worktree)
    if not key:
        return {"mode": "unmanaged", "ticket": None}
    with cohort._lease(cfg) as acquired:
        if not acquired:
            raise ValueError("ownership controller busy; retry the handoff")
        tdir = cfg.ticket_dir(key)
        if not owned(tdir):
            raise ValueError(f"{key} is not under interactive control")
        record = _read(tdir)
        if session is not None and record.get("session", "") != session:
            raise ValueError(f"{key} is controlled by another Pi session")
        if record["mode"] != "interactive":
            raise ValueError(f"{key}'s live runner has not yielded yet")
        lease = locks.Lease(cfg.state_root / "locks" / f"runner-{key.lower()}", "interactive-return")
        if not lease.acquire(hold=True):
            raise ValueError(f"{key}'s runner is still live")
        try:
            # Queue before dropping foreground ownership, so a crash cannot create an unowned ticket.
            (tdir / "PARKED.json").write_text(json.dumps({"admission_sequence": record["admission_sequence"]}))
            (tdir / "PARKED").touch()
            ticket = state.load(tdir)
            if ticket.state in ("gates", "draft-pr", "ready", "bot-loop", "human-gate-1", "colleague-loop", "human-gate-2"):
                ticket.previous, ticket.state = ticket.state, "gates"
                ticket.evidence_sha = ""
                state.save(tdir, ticket)
                ci_path = tdir / "ci.json"
                if ci_path.exists():
                    ci = json.loads(ci_path.read_text())
                    for name in ("ready_marked_sha", "bot_wait_since", "bot_retriggered"):
                        ci.pop(name, None)
                    ci_path.write_text(json.dumps(ci))
            (tdir / MARKER).unlink()
            return {"mode": "human" if (tdir / "HUMAN").exists() else "queued", "ticket": key}
        finally:
            lease.release()
