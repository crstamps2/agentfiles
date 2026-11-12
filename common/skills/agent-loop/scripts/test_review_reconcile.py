"""External completion must release only a verified obsolete reservation."""
import copy
import importlib
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cohort
import config
import locks
import recovery
import state
from test_config import MINI


class ReviewedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        path = self.root / 'config.toml'
        path.write_text(MINI.format(root=str(self.root / 'state')) + '\n[cohort]\ncapacity = 2\n')
        self.cfg = config.load(path)
        self.cfg.ensure_dirs()
        self.wt = self.root / 'wt'
        self.wt.mkdir()
        for args in [('init', '-q', '-b', 'internal/zip-1-example'), ('config', 'user.email', 'test@example.test'),
                     ('config', 'user.name', 'Test')]:
            self.git(*args)
        (self.wt / 'example.txt').write_text('committed\n')
        self.git('add', 'example.txt')
        self.git('commit', '-qm', 'original checkpoint')
        self.checkpoint = self.git('rev-parse', 'HEAD').strip()
        (self.wt / 'example.txt').write_text('completed work\n')
        self.git('add', 'example.txt')
        self.git('commit', '-qm', 'completed work')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.git('remote', 'add', 'origin', 'git@github.com:example/application.git')
        (self.wt / 'example.txt').write_text('user edits must survive\n')
        value = cohort.initialize(self.cfg)
        value.slots = [cohort.Slot(slot=1, ticket='ZIP-1', phase='active', admission_sequence=1, source='drip'),
                       cohort.Slot(slot=2, ticket='ZIP-2', phase='active', admission_sequence=2, source='drip')]
        value.next_admission_sequence = 3
        cohort._save_unlocked(self.cfg, value)
        contract = recovery.new_contract('ZIP-1', {'state':'implement', 'head_sha':self.checkpoint},
                                        {'kind':'resume-stage', 'state':'implement'}, ['example-defect'])
        state.save(self.cfg.ticket_dir('ZIP-1'), state.Ticket(key='ZIP-1', state='paused', previous='implement',
                   worktree=str(self.wt), branch='internal/zip-1-example', reason='old environment failure',
                   recovery={'active':contract, 'queued':[]}))
        state.save(self.cfg.ticket_dir('ZIP-2'), state.Ticket(key='ZIP-2', state='blocked', reason='owner question'))
        self.raw = {'jira': {'apiVersion':'v2', 'command':'jira.workitem.get',
                            'data':[{'key':'ZIP-1', 'status':{'name':'In Review'}}],
                            'meta':{'acquisition':{'requested':1,'returned':1,'succeeded':1,'failed':0,'omitted':0,'truncated':0},
                                    'evidence':{'coverage':{'status':'complete'}}}},
                    'pr': {'number':12,'state':'OPEN','isDraft':False,'headRefOid':self.head,
                           'headRefName':'internal/zip-1-example','url':'https://github.com/example/application/pull/12',
                           'statusCheckRollup':[{'__typename':'CheckRun','name':'tests','status':'COMPLETED','conclusion':'SUCCESS'},
                                                {'__typename':'CheckRun','name':'optional','status':'COMPLETED','conclusion':'SKIPPED'},
                                                {'__typename':'StatusContext','context':'coverage','state':'SUCCESS'}]}}

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.wt), *args], text=True, stderr=subprocess.STDOUT)

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('review_reconcile'),
                             'missing verified external-completion reconciliation; stale reservation cannot release')
        return importlib.import_module('review_reconcile')

    def release(self, lookup=None):
        return self.module().release_reviewed(self.cfg, 'ZIP-1', lookup=lookup or (lambda *args: copy.deepcopy(self.raw)))

    def unchanged_on_refusal(self):
        before = (self.cfg.state_root / 'cohort.json').read_bytes()
        with self.assertRaises(ValueError):
            self.release()
        self.assertEqual((self.cfg.state_root / 'cohort.json').read_bytes(), before)

    def test_verified_external_completion_frees_one_slot_without_resuming_or_rewriting_user_work(self):
        before = {p:p.read_bytes() for p in (self.cfg.ticket_dir('ZIP-1')/'state.json',
                                           self.cfg.ticket_dir('ZIP-2')/'state.json',self.wt/'example.txt')}
        receipt = self.release()
        self.assertEqual(receipt['pr'], 12)
        self.assertEqual(cohort.members(self.cfg), ['ZIP-2'])
        self.assertEqual(cohort.load(self.cfg).history[-1]['event'], 'released')
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_failed_draft_pending_empty_or_unverified_checks_cannot_release(self):
        changes = [{'isDraft':True}, {'state':'CLOSED'}, {'headRefOid':'b'*40}, {'number':True},
                   {'headRefName':'other-branch'}, {'url':'https://github.com/other/repository/pull/12'},
                   {'statusCheckRollup':[]}, {'statusCheckRollup':[{'__typename':'CheckRun','status':'COMPLETED','conclusion':'FAILURE'}]},
                   {'statusCheckRollup':[{'__typename':'CheckRun','status':'IN_PROGRESS','conclusion':''}]},
                   {'statusCheckRollup':[{'__typename':'StatusContext','state':'PENDING'}]}, {'isDraft':'false'}]
        original = copy.deepcopy(self.raw['pr'])
        for change in changes:
            with self.subTest(change=change):
                self.raw['pr'] = {**original, **change}
                self.unchanged_on_refusal()
        self.raw['pr'] = original

    def test_jira_identity_status_or_incomplete_results_cannot_release(self):
        changes = [{'data':[{'key':'ZIP-1','status':{'name':'In Progress'}}]},
                   {'data':[{'key':'ZIP-2','status':{'name':'In Review'}}]}, {'data':[]},
                   {'meta':{'acquisition':{'failed':1}}}]
        original = copy.deepcopy(self.raw['jira'])
        for change in changes:
            with self.subTest(change=change):
                self.raw['jira'] = {**original, **change}
                self.unchanged_on_refusal()
        for field, invalid in [('failed',1), ('omitted',1), ('truncated',1), ('returned',0), ('requested',True)]:
            with self.subTest(field=field):
                self.raw['jira'] = copy.deepcopy(original)
                self.raw['jira']['meta']['acquisition'][field] = invalid
                self.unchanged_on_refusal()
        self.raw['jira'] = copy.deepcopy(original)
        self.raw['jira']['meta']['evidence']['coverage']['status'] = 'partial'
        self.unchanged_on_refusal()
        self.raw['jira'] = original

    def test_current_recovery_or_pending_decision_cannot_be_overridden(self):
        path = self.cfg.ticket_dir('ZIP-1') / 'state.json'
        t = state.load(path.parent)
        t.recovery['active']['checkpoint']['head_sha'] = self.head
        state.save(path.parent, t)
        self.unchanged_on_refusal()
        t.recovery['active']['checkpoint']['head_sha'] = self.checkpoint
        t.decision_request = {'question':'needs owner'}
        state.save(path.parent, t)
        self.unchanged_on_refusal()

    def test_unstructured_owner_question_or_pending_candidate_cannot_release(self):
        ticket = state.load(self.cfg.ticket_dir('ZIP-1'))
        ticket.reason = 'need Cody: product choice is unresolved'
        state.save(self.cfg.ticket_dir('ZIP-1'), ticket)
        self.unchanged_on_refusal()
        ticket.reason = 'old environment failure'
        state.save(self.cfg.ticket_dir('ZIP-1'), ticket)
        (self.cfg.ticket_dir('ZIP-1')/'decision-candidate.json').write_text('{}')
        self.unchanged_on_refusal()

    def test_expired_observation_and_concurrent_head_change_preserve_reservation(self):
        module = self.module()
        clock = [0.0]
        def expired(*args):
            clock[0] = 31.0
            return copy.deepcopy(self.raw)
        before = (self.cfg.state_root/'cohort.json').read_bytes()
        with patch.object(module.time, 'monotonic', side_effect=lambda: clock[0]), self.assertRaises(ValueError):
            self.release(expired)
        self.assertEqual((self.cfg.state_root/'cohort.json').read_bytes(), before)
        def changed(*args):
            self.git('add', 'example.txt')
            self.git('commit', '-qm', 'concurrent owner change')
            return copy.deepcopy(self.raw)
        with self.assertRaises(ValueError):
            self.release(changed)
        self.assertEqual((self.cfg.state_root/'cohort.json').read_bytes(), before)

    def test_foreground_markers_and_live_writer_preserve_reservation(self):
        for name in ('HUMAN','INTERACTIVE.json','PARKED','PARKED.json'):
            mark = self.cfg.ticket_dir('ZIP-1') / name
            mark.write_text('uncertain ownership')
            self.unchanged_on_refusal()
            mark.unlink()
        lease = locks.Lease(self.cfg.state_root/'locks/runner-zip-1', 'test writer')
        self.assertTrue(lease.acquire(hold=True))
        try:
            self.unchanged_on_refusal()
        finally:
            lease.release()

    def test_remote_truth_runs_outside_ownership_lease_and_rechecks_local_identity(self):
        def lookup(*args):
            self.assertFalse(locks.is_held(self.cfg.state_root/'locks/cohort'))
            t = state.load(self.cfg.ticket_dir('ZIP-1'))
            t.branch = 'changed-by-owner'
            state.save(self.cfg.ticket_dir('ZIP-1'), t)
            return copy.deepcopy(self.raw)
        before = (self.cfg.state_root/'cohort.json').read_bytes()
        with self.assertRaises(ValueError):
            self.release(lookup)
        self.assertEqual((self.cfg.state_root/'cohort.json').read_bytes(), before)

    def test_live_pid_record_writer_is_not_overwritten(self):
        lease = locks.Lease(self.cfg.state_root/'locks/runner-zip-1', 'legacy live writer')
        self.assertTrue(lease.acquire())
        before = lease.path.read_bytes()
        try:
            self.unchanged_on_refusal()
            self.assertEqual(lease.path.read_bytes(), before)
        finally:
            lease.release()

    def test_live_pid_record_controller_is_not_overwritten(self):
        lease = locks.Lease(self.cfg.state_root/'locks/cohort', 'legacy live controller')
        self.assertTrue(lease.acquire())
        before = lease.path.read_bytes()
        try:
            self.unchanged_on_refusal()
            self.assertEqual(lease.path.read_bytes(), before)
        finally:
            lease.release()

    def test_structured_owner_hold_with_neutral_reason_cannot_release(self):
        ticket = state.load(self.cfg.ticket_dir('ZIP-1'))
        ticket.needs_operator = 'owner question'
        state.save(self.cfg.ticket_dir('ZIP-1'), ticket)
        self.unchanged_on_refusal()

    def test_uncertain_legacy_boot_identity_cannot_be_reclaimed(self):
        lease = self.cfg.state_root/'locks/runner-zip-1'
        lease.write_text(json.dumps({'pid':1, 'boot_id':True}))
        self.unchanged_on_refusal()

    def test_rebased_checkpoint_can_be_superseded_by_current_review_proof(self):
        old = self.git('commit-tree', self.git('rev-parse', 'HEAD^{tree}').strip(),
                       '-m', 'old manually rewritten history').strip()
        ticket = state.load(self.cfg.ticket_dir('ZIP-1'))
        ticket.recovery['active']['checkpoint']['head_sha'] = old
        state.save(self.cfg.ticket_dir('ZIP-1'), ticket)
        receipt = self.release()
        self.assertEqual(receipt['checkpoint_relation'], 'divergent')
        self.assertEqual(receipt['checkpoint_head_sha'], old)
        row = cohort.load(self.cfg).history[-1]
        self.assertEqual(row['reason'], 'verified-external-review')
        self.assertEqual(row['review_evidence']['obsolete_recovery_id'], ticket.recovery['active']['id'])

    def test_changed_origin_cannot_reuse_old_repository_proof(self):
        def changed(*args):
            self.git('remote', 'set-url', 'origin', 'git@github.com:other/application.git')
            return copy.deepcopy(self.raw)
        before = (self.cfg.state_root/'cohort.json').read_bytes()
        with self.assertRaises(ValueError):
            self.release(changed)
        self.assertEqual((self.cfg.state_root/'cohort.json').read_bytes(), before)

    def test_nonexistent_checkpoint_cannot_create_capacity(self):
        ticket = state.load(self.cfg.ticket_dir('ZIP-1'))
        ticket.recovery['active']['checkpoint']['head_sha'] = 'f' * 40
        state.save(self.cfg.ticket_dir('ZIP-1'), ticket)
        self.unchanged_on_refusal()

    def test_future_checkpoint_cannot_be_declared_obsolete(self):
        future = self.git('commit-tree', self.git('rev-parse', 'HEAD^{tree}').strip(),
                          '-p', self.head, '-m', 'future unfinished work').strip()
        ticket = state.load(self.cfg.ticket_dir('ZIP-1'))
        ticket.recovery['active']['checkpoint']['head_sha'] = future
        state.save(self.cfg.ticket_dir('ZIP-1'), ticket)
        self.unchanged_on_refusal()

    def test_externally_reviewed_recovery_cannot_be_readmitted_and_replayed(self):
        self.release()
        before = (self.cfg.state_root/'cohort.json').read_bytes()
        with self.assertRaises(cohort.CohortError):
            cohort.reserve(self.cfg, 'ZIP-1', 'drip')
        self.assertEqual((self.cfg.state_root/'cohort.json').read_bytes(), before)
        cohort.reserve(self.cfg, 'ZIP-3', 'drip')
        self.assertEqual(cohort.members(self.cfg), ['ZIP-3', 'ZIP-2'])

    def test_failed_atomic_save_preserves_old_reservation_and_history(self):
        module = self.module()
        before = (self.cfg.state_root/'cohort.json').read_bytes()
        with patch.object(module.cohort, '_save_unlocked', side_effect=OSError('injected write failure')), self.assertRaises(OSError):
            self.release()
        self.assertEqual((self.cfg.state_root/'cohort.json').read_bytes(), before)

    def test_actual_cli_parses_complete_twgv2_and_gh_payloads(self):
        module = self.module()
        real_run = subprocess.run
        def run(argv, **kwargs):
            if argv[0] == 'gh':
                return subprocess.CompletedProcess(argv, 0, json.dumps(self.raw['pr']), '')
            if argv[0] == 'twg':
                self.assertIn('--output-summary', argv)
                self.assertIn('none', argv)
                return subprocess.CompletedProcess(argv, 0, json.dumps(self.raw['jira']), '')
            return real_run(argv, **kwargs)
        with patch.object(module.subprocess, 'run', side_effect=run):
            self.assertEqual(module.main(['--config',str(self.root/'config.toml'),'--ticket','ZIP-1']), 0)
        self.assertEqual(cohort.members(self.cfg), ['ZIP-2'])


if __name__ == '__main__':
    unittest.main()
