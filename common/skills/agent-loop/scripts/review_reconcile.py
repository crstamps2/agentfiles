"""Explicit operator reconciliation of externally reviewed, obsolete repair waits.

This releases an active reservation, not a lifecycle gate: ticket state, recovery,
user files and foreground ownership remain untouched. Normal admission stays guarded.
"""
import argparse
import dataclasses
import json
import pathlib
import re
import subprocess
import time

import cohort
import config
import locks
import recovery
import state
from lifecycle import OWNER_QUESTION


def _git(wt, *args):
    result = subprocess.run(['git', '-C', str(wt), *args], capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise ValueError('cannot verify current worktree identity')
    return result.stdout.strip()


def _read_json(argv, wt, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError('external review observation expired')
    result = subprocess.run(argv, cwd=wt, capture_output=True, text=True, timeout=remaining)
    if result.returncode:
        raise ValueError(f'{argv[0]} could not verify external review')
    return json.loads(result.stdout)


def read_external(key, wt, deadline):
    pr = _read_json(['gh', 'pr', 'view', '--json',
                     'number,state,isDraft,headRefOid,headRefName,url,statusCheckRollup'], wt, deadline)
    jira = _read_json(['twg', 'jira', 'workitem', 'get', key, '--fields', 'status',
                       '--output-summary', 'none', '-o', 'json'], wt, deadline)
    return {'jira': jira, 'pr': pr}


def _verify_external(raw, key, branch, head, repo):
    try:
        jira, pr = raw['jira'], raw['pr']
        acquisition = jira['meta']['acquisition']
        expected = {'requested':1, 'returned':1, 'succeeded':1, 'failed':0, 'omitted':0, 'truncated':0}
        if (jira['apiVersion'] != 'v2' or jira['command'] != 'jira.workitem.get'
                or any(type(acquisition.get(k)) is not int or acquisition[k] != v for k, v in expected.items())
                or jira['meta']['evidence']['coverage']['status'] != 'complete'
                or len(jira['data']) != 1 or jira['data'][0]['key'] != key
                or jira['data'][0]['status']['name'] not in ('Ready for Review', 'In Review')):
            raise ValueError('Jira does not verify this ticket is already in review')
        number = pr['number']
        if (type(number) is not int or number < 1 or pr['state'] != 'OPEN' or pr['isDraft'] is not False
                or pr['headRefOid'] != head or pr['headRefName'] != branch
                or pr['url'] != f'https://github.com/{repo}/pull/{number}'):
            raise ValueError('current PR identity or readiness is not verified')
        checks = pr['statusCheckRollup']
        if not isinstance(checks, list) or not checks:
            raise ValueError('current PR has no verified checks')
        successful = False
        for check in checks:
            if check.get('__typename') == 'CheckRun':
                if check.get('status') != 'COMPLETED' or check.get('conclusion') not in ('SUCCESS', 'SKIPPED', 'NEUTRAL'):
                    raise ValueError('current PR checks are not successful')
                successful = successful or check['conclusion'] == 'SUCCESS'
            elif check.get('__typename') == 'StatusContext' and check.get('state') == 'SUCCESS':
                successful = True
            else:
                raise ValueError('unknown or incomplete PR check')
        if not successful:
            raise ValueError('current PR has no successful check')
        return number, jira['data'][0]['status']['name']
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ValueError('malformed external review evidence') from exc


def _eligible(ticket, tdir, head):
    if any((tdir/name).exists() or (tdir/name).is_symlink()
           for name in ('HUMAN', 'INTERACTIVE.json', 'PARKED', 'PARKED.json')):
        raise ValueError('foreground ownership must be preserved')
    candidate = tdir/'decision-candidate.json'
    if (ticket.state != 'paused' or ticket.needs_operator != '' or ticket.decision_request
            or OWNER_QUESTION.search(ticket.reason or '')
            or candidate.exists() or candidate.is_symlink()):
        raise ValueError('not an obsolete paused repair wait without owner questions')
    try:
        active = recovery.validate_contract(ticket.recovery['active'])
        queued = ticket.recovery.get('queued')
        if (not isinstance(queued, list) or queued or active['ticket'] != ticket.key or active['status'] != 'waiting-repair'
                or active['resume'] != {'kind':'resume-stage', 'state':'implement'}
                or not re.fullmatch(r'[0-9a-f]{40}', active['checkpoint'].get('head_sha', ''))
                or active['checkpoint']['head_sha'] == head):
            raise ValueError('current or queued recovery cannot be superseded')
    except (KeyError, TypeError, recovery.RecoveryError) as exc:
        raise ValueError('repair identity is not verified') from exc


def _repository_identity(wt):
    root = pathlib.Path(_git(wt, 'rev-parse', '--show-toplevel')).resolve()
    gitdir = pathlib.Path(_git(wt, 'rev-parse', '--absolute-git-dir')).resolve()
    if root != wt.resolve():
        raise ValueError('worktree root identity does not match ticket')
    remote = _git(wt, 'remote', 'get-url', 'origin').removesuffix('.git').rstrip('/')
    match = re.fullmatch(r'(?:git@github\.com:|https://github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)', remote)
    if not match:
        raise ValueError('unverified repository origin')
    return (match[1], root.stat().st_dev, root.stat().st_ino,
            gitdir.stat().st_dev, gitdir.stat().st_ino)


def _ancestor(wt, older, newer):
    result = subprocess.run(['git', '-C', str(wt), 'merge-base', '--is-ancestor', older, newer],
                            capture_output=True, text=True, timeout=3)
    if result.returncode not in (0, 1):
        raise ValueError('checkpoint lineage is unverified')
    return result.returncode == 0


def release_reviewed(cfg, key, *, lookup=None):
    if not re.fullmatch(r'[A-Z][A-Z0-9]*-[1-9][0-9]*', key):
        raise ValueError('invalid ticket identity')
    tdir = cfg.ticket_dir(key)
    ticket = state.load(tdir)
    if ticket.key != key or not ticket.worktree or not pathlib.Path(ticket.worktree).is_dir():
        raise ValueError('missing or mismatched ticket/worktree identity')
    wt = pathlib.Path(ticket.worktree)
    head = _git(wt, 'rev-parse', 'HEAD')
    branch = _git(wt, 'branch', '--show-current')
    if (not re.fullmatch(r'[0-9a-f]{40}', head) or ticket.branch != branch
            or not re.search(rf'(?<![a-z0-9]){re.escape(key.lower())}(?![0-9])', branch.lower())):
        raise ValueError('current branch does not match the ticket')
    repository = _repository_identity(wt)
    _eligible(ticket, tdir, head)
    checkpoint = ticket.recovery['active']['checkpoint']['head_sha']
    if _git(wt, 'rev-parse', '--is-shallow-repository') != 'false':
        raise ValueError('checkpoint history is incomplete; fetch full history first')
    if _git(wt, 'cat-file', '-t', checkpoint) != 'commit' or _ancestor(wt, head, checkpoint):
        raise ValueError('checkpoint is invalid or contains newer unfinished work')
    relation = 'ancestor' if _ancestor(wt, checkpoint, head) else 'divergent'
    deadline = time.monotonic() + 30
    raw = (lookup or read_external)(key, wt, deadline)
    pr, jira_status = _verify_external(raw, key, branch, head, repository[0])
    identity = dataclasses.asdict(ticket)
    with cohort._lease(cfg, respect_pid_owner=True) as acquired:
        if not acquired:
            raise ValueError('ownership controller busy')
        writer = locks.Lease(cfg.state_root/'locks'/f'runner-{key.lower()}', 'external-review-reconciliation')
        if not writer.acquire(hold=True, respect_pid_owner=True):
            raise ValueError('ticket writer is still live')
        try:
            current = state.load(tdir)
            _eligible(current, tdir, head)
            if (dataclasses.asdict(current) != identity or _git(wt, 'rev-parse', 'HEAD') != head
                    or _git(wt, 'branch', '--show-current') != branch
                    or _repository_identity(wt) != repository or time.monotonic() >= deadline):
                raise ValueError('ticket changed or observations expired; reverify')
            value = cohort._load_unlocked(cfg)
            slot = cohort._find(value, key)
            if slot is None or slot.phase != 'active':
                raise ValueError('ticket has no active reservation to reconcile')
            cohort._release(value, slot, pr, 'verified-external-review')
            proof = {'head_sha':head, 'branch':branch, 'jira_status':jira_status,
                     'pr_url':raw['pr']['url'], 'obsolete_recovery_id':current.recovery['active']['id'],
                     'checkpoint_head_sha':checkpoint, 'checkpoint_relation':relation}
            value.history[-1]['review_evidence'] = proof
            cohort._save_unlocked(cfg, value)
            return {'ticket':key, 'pr':pr, **proof}
        finally:
            writer.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--ticket', required=True)
    args = parser.parse_args(argv)
    cfg = config.load(args.config) if args.config else config.load()
    try:
        receipt = release_reviewed(cfg, args.ticket)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f'not reconciled: {exc}')
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
