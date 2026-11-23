"""Owner-side retirement for explicit local spindown; never resume, admit, or edit another ticket."""
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

import attempt
import cohort
import config
import interactive
import locks
import reconcile
import state
import supervise
import workspace_binding


def supervisor_absent(key):
    label = supervise.label(key)
    p = subprocess.run(['/bin/launchctl', 'print', f'gui/{os.getuid()}/{label}'],
                       capture_output=True, text=True, timeout=5, check=False)
    return (p.returncode == 113 and f'Could not find service "{label}"' in p.stderr
            and not supervise.plist_path(key).exists())


def inspect(cfg, key, path, workspace_id, branch, *, locked=False):
    path = pathlib.Path(path)
    if (not re.fullmatch(r'[A-Z][A-Z0-9]*-[1-9][0-9]*', key) or path.name != key.lower()
            or path != path.resolve() or not path.is_dir() or not branch
            or not workspace_id.endswith('::' + str(path))):
        raise ValueError("invalid retirement identity")
    td = cfg.ticket_dir(key)
    if td.resolve() != td or (td/'state.json').is_symlink() or not (td/'state.json').is_file():
        raise ValueError("unverified owner state")
    t = state.load(td)
    if (t.key, t.worktree, t.branch) != (key, str(path), branch):
        raise ValueError("ticket state does not own this exact checkout and branch")
    binding = workspace_binding.read(td)
    if binding and (binding.backend, binding.key, binding.worktree, binding.branch, binding.workspace_id,
                    binding.repo_id + "::" + str(path)) != ("orca", key, str(path), branch, workspace_id, workspace_id):
        raise ValueError("binding identifies a different workspace")
    if os.path.lexists(td/'HUMAN'):
        raise ValueError("legacy human ownership: explicit owner handoff required")
    foreground = None
    if os.path.lexists(td/interactive.MARKER):
        if (td/interactive.MARKER).is_symlink() or not binding:
            raise ValueError("unverified foreground workspace")
        foreground = interactive._read(td)
        if (foreground.get('ticket') != key or foreground['mode'] != 'interactive'
                or not foreground.get('session')):
            raise ValueError("unsettled foreground ownership: wait for handoff")
    if any(os.path.lexists(td / f) for f in ('PARKED', 'PARKED.json', 'workspace.retired.json', 'INTERACTIVE.retired.json')):
        raise ValueError("pending handoff or previous retirement: inspect before retry")
    if t.state not in state.STATES or t.state == "retired" or t.retired_workspace:
        raise ValueError("invalid lifecycle state or prior retirement")
    lease = cfg.state_root / 'locks' / ('runner-' + key.lower())
    if not locked and (os.path.lexists(lease) or locks.is_held(lease)):
        raise ValueError("ticket writer has not yielded")
    if not supervisor_absent(key):
        raise ValueError("ticket supervisor is loaded or unverifiable")
    for leaf in reconcile._leaf_dirs(cfg.state_root / 'attempts' / key):
        rec = attempt.load(leaf)
        if rec.status not in attempt.TERMINAL:
            raise ValueError("unfinished ticket attempt; writer handoff required")
    if (cfg.state_root/'cohort.json').exists() and cohort._find(cohort.load(cfg), key):
        raise ValueError("active cohort slot: owner handoff required")
    encoded = json.dumps([dataclasses.asdict(t), dataclasses.asdict(binding) if binding else None, foreground], sort_keys=True)
    return {"key": key, "path": str(path), "branch": branch, "workspace_id": workspace_id,
            "digest": hashlib.sha256(encoded.encode()).hexdigest()}


def commit(cfg, plan):
    key = plan['key']
    if not re.fullmatch(r'[A-Z][A-Z0-9]*-[1-9][0-9]*', key):
        raise ValueError("invalid ticket key")
    with cohort._lease(cfg, respect_pid_owner=True) as acquired:
        if not acquired:
            raise ValueError("cohort ownership is busy; nothing retired")
        lease = locks.Lease(cfg.state_root/'locks'/('runner-' + key.lower()), 'retire-workspace')
        if not lease.acquire(hold=True, respect_pid_owner=True):
            raise ValueError("live ticket writer; nothing retired")
        try:
            if plan != inspect(cfg, key, plan['path'], plan['workspace_id'], plan['branch'], locked=True):
                raise ValueError("workspace retirement plan changed")
            td = cfg.ticket_dir(key)
            t = state.load(td)
            t.retired_workspace = {**plan, "prior_state": t.state, "reason": t.reason,
                                   "recovery": t.recovery, "workspace": t.workspace}
            t.previous, t.state = t.state, "retired"
            t.worktree = t.workspace = ""
            t.recovery = {"active": None, "queued": []}
            t.reason = "Operator requested permanent local workspace teardown"
            state.save(td, t)
            # Fence future dispatch first. A crash before archiving the binding
            # leaves the checkout protected, never unowned and still runnable.
            if interactive.owned(td):
                os.rename(td/interactive.MARKER, td/'INTERACTIVE.retired.json')
            if workspace_binding.read(td):
                os.rename(td/'workspace.json', td/'workspace.retired.json')
        finally:
            lease.release()


def main():
    op, raw = sys.argv[1:]
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError('invalid workspace retirement request') from exc
    cfg = config.load()
    if str(cfg.state_root) != plan.pop('root'):
        raise ValueError("retirement request belongs to another state root")
    if op == 'inspect':
        print(json.dumps(inspect(cfg, **plan)))
    elif op == 'commit':
        commit(cfg, plan)
        print(json.dumps({"retired": plan['key']}))
    else:
        raise ValueError("invalid operation")


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
