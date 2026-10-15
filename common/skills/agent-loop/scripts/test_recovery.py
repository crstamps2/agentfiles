import copy
import pathlib
import tempfile
import unittest

import config
import recovery
import state


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        source = pathlib.Path(__file__).resolve().parent.parent / "hopper.toml"
        text = source.read_text()
        text = text.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
        text = text.replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{root}/agents"')
        path = root / "hopper.toml"
        path.write_text(text)
        self.cfg = config.load(path)
        self.cfg.ensure_dirs()
        self.key = "ZIP-1"
        self.tdir = self.cfg.ticket_dir(self.key)
        ticket = state.Ticket(key=self.key, state="blocked", previous="implement", worktree=str(root / "wt"))
        ticket.attempts[f"{self.key}/001"] = [{"n": 1, "outcome": "accepted", "slug": "component"}]
        state.save(self.tdir, ticket)

    def tearDown(self):
        self.tmp.cleanup()

    def contract(self, **changes):
        checkpoint = {
            "state": "implement",
            "previous": "plan-review",
            "head_sha": "abc123",
            "task_id": "001",
            "task_slug": "component",
        }
        resume = {"kind": "retry-task", "state": "implement", "task_id": "001", "task_slug": "component"}
        checkpoint.update(changes.pop("checkpoint", {}))
        resume.update(changes.pop("resume", {}))
        return recovery.new_contract(self.key, checkpoint, resume, ("defect-1",), **changes)

    def test_future_contract_schema_is_rejected_without_mutating_payload(self):
        payload = self.contract()
        payload["schema_version"] = 2
        before = copy.deepcopy(payload)
        with self.assertRaises(recovery.RecoveryError):
            recovery.validate_contract(payload)
        self.assertEqual(payload, before)

    def test_resume_kinds_enforce_their_targets(self):
        cases = {
            "resume-stage": "gates",
            "republish": "implement",
            "replan": "plan",
            "retry-spinup": "spinup",
            "wait-provider": "implement",
        }
        for kind, target in cases.items():
            with self.subTest(kind=kind):
                contract = recovery.new_contract(
                    self.key,
                    {"state": target, "head_sha": "abc123"},
                    {"kind": kind, "state": target},
                )
                self.assertEqual(recovery.validate_contract(contract)["resume"]["kind"], kind)

        invalid = recovery.new_contract(
            self.key,
            {"state": "implement", "head_sha": "abc123"},
            {"kind": "republish", "state": "implement"},
        )
        invalid["resume"]["state"] = "gates"
        with self.assertRaises(recovery.RecoveryError):
            recovery.validate_contract(invalid)

    def test_retry_task_requires_matching_task_identity(self):
        invalid = self.contract()
        invalid["resume"]["task_slug"] = "other"
        with self.assertRaises(recovery.RecoveryError):
            recovery.validate_contract(invalid)

    def test_activate_is_idempotent_and_queue_is_fifo(self):
        first = self.contract(contract_id="recovery-1")
        second = recovery.new_contract(
            self.key,
            {"state": "gates", "head_sha": "abc123"},
            {"kind": "resume-stage", "state": "gates"},
            contract_id="recovery-2",
        )
        recovery.activate(self.cfg, self.key, first)
        recovery.activate(self.cfg, self.key, first)
        recovery.queue(self.cfg, self.key, second)
        recovery.queue(self.cfg, self.key, second)
        ticket = state.load(self.tdir)
        self.assertEqual(ticket.recovery["active"]["id"], "recovery-1")
        self.assertEqual([row["id"] for row in ticket.recovery["queued"]], ["recovery-2"])

    def test_mark_ready_records_event_once(self):
        recovery.activate(self.cfg, self.key, self.contract(contract_id="recovery-1"))
        first = recovery.mark_ready(self.cfg, self.key, "event-1")
        second = recovery.mark_ready(self.cfg, self.key, "event-1")
        self.assertEqual(first.recovery["active"]["status"], "ready-to-resume")
        self.assertEqual(second.recovery["active"]["last_event_id"], "event-1")

    def test_resume_active_invokes_only_the_stored_task_action(self):
        recovery.activate(self.cfg, self.key, self.contract(contract_id="recovery-1"))
        recovery.mark_ready(self.cfg, self.key, "event-1")
        retries = []
        supervisors = []

        def retry_task(cfg, key, task_id, note=""):
            retries.append((key, task_id, note))
            ticket = state.load(cfg.ticket_dir(key))
            ticket = state.resume_target(ticket, "implement", reason=note)
            state.save(cfg.ticket_dir(key), ticket)

        result = recovery.resume_active(
            self.cfg,
            self.key,
            retry_task=retry_task,
            ensure_supervisor=lambda key, worktree, root: supervisors.append((key, worktree, root)),
        )

        self.assertEqual(result, "resumed")
        self.assertEqual([(key, task) for key, task, _ in retries], [(self.key, "001")])
        self.assertEqual([row[0] for row in supervisors], [self.key])
        ticket = state.load(self.tdir)
        self.assertEqual(ticket.state, "implement")
        self.assertEqual(ticket.recovery["active"], None)
        self.assertEqual(ticket.recovery["queued"], [])

    def test_resume_active_obsoletes_a_changed_task_slug(self):
        contract = self.contract(contract_id="recovery-1")
        recovery.activate(self.cfg, self.key, contract)
        recovery.mark_ready(self.cfg, self.key, "event-1")
        ticket = state.load(self.tdir)
        ticket.attempts[f"{self.key}/001"][-1]["slug"] = "replacement"
        state.save(self.tdir, ticket)

        result = recovery.resume_active(
            self.cfg,
            self.key,
            retry_task=lambda *args, **kwargs: self.fail("obsolete recovery retried the task"),
            ensure_supervisor=lambda *args: self.fail("obsolete recovery started the supervisor"),
        )

        self.assertEqual(result, "obsolete")
        self.assertEqual(state.load(self.tdir).recovery["active"], None)


if __name__ == "__main__":
    unittest.main()
