import copy
import json
import pathlib
import tempfile
import unittest

import config
import recovery
import state


class Crash(BaseException):
    """A process kill: never swallowed by the per-row error isolation."""


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        source = pathlib.Path(__file__).resolve().parent.parent / "hopper.toml"
        text = source.read_text().replace("\ncapacity = 2 ", "\ncapacity = 4 ")   # these tests exercise four slots
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

    def event_ticket(self, key, recovery_id):
        tdir = self.cfg.ticket_dir(key)
        state.save(tdir, state.Ticket(key=key, state="blocked", previous="implement", worktree=f"/tmp/{key.lower()}"))
        contract = recovery.new_contract(
            key,
            {"state": "implement", "head_sha": "abc123"},
            {"kind": "resume-stage", "state": "implement"},
            ("defect-1",),
            contract_id=recovery_id,
        )
        recovery.activate(self.cfg, key, contract)
        return {"ticket": key, "recovery_id": recovery_id}

    def test_one_landed_event_resumes_every_affected_ticket(self):
        affected = [self.event_ticket("ZIP-2", "recovery-2"), self.event_ticket("ZIP-3", "recovery-3")]
        event = recovery.emit_event(self.cfg, {"id": "defect-1", "affected_tickets": affected}, "landed", "abc123")
        supervisors = []
        results = recovery.consume_events(
            self.cfg,
            retry_task=lambda *args, **kwargs: self.fail("resume-stage called retry-task"),
            ensure_supervisor=lambda key, *args: supervisors.append(key),
        )
        self.assertEqual(results, ["ZIP-2:resumed", "ZIP-3:resumed"])
        self.assertEqual(supervisors, ["ZIP-2", "ZIP-3"])
        self.assertEqual(state.load(self.cfg.ticket_dir("ZIP-2")).state, "implement")
        self.assertFalse(event.exists())
        self.assertTrue((self.cfg.state_root / "defects" / "events" / "processed" / event.name).exists())

    def test_not_needed_event_also_resumes_a_matching_contract(self):
        affected = [self.event_ticket("ZIP-2", "recovery-2")]
        recovery.emit_event(self.cfg, {"id": "defect-1", "affected_tickets": affected}, "not-needed")
        results = recovery.consume_events(
            self.cfg,
            retry_task=lambda *args, **kwargs: None,
            ensure_supervisor=lambda *args: None,
        )
        self.assertEqual(results, ["ZIP-2:resumed"])

    def test_partial_event_delivery_replays_without_duplicate_resume(self):
        affected = [self.event_ticket("ZIP-2", "recovery-2"), self.event_ticket("ZIP-3", "recovery-3")]
        event = recovery.emit_event(self.cfg, {"id": "defect-1", "affected_tickets": affected}, "landed", "abc123")
        supervisors = []
        calls = 0

        def crash_after_first(*_):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise Crash("crash after first delivery")

        with self.assertRaises(Crash):
            recovery.consume_events(
                self.cfg,
                retry_task=lambda *args, **kwargs: None,
                ensure_supervisor=lambda key, *args: supervisors.append(key),
                after_apply=crash_after_first,
            )
        self.assertTrue(event.exists())
        results = recovery.consume_events(
            self.cfg,
            retry_task=lambda *args, **kwargs: None,
            ensure_supervisor=lambda key, *args: supervisors.append(key),
        )
        self.assertEqual(results, ["ZIP-2:obsolete", "ZIP-3:resumed"])
        self.assertEqual(supervisors, ["ZIP-2", "ZIP-3"])

    def test_retry_side_effect_is_not_duplicated_after_a_crash(self):
        import heal
        import ladder
        import runner
        contract = self.contract(contract_id="retry-recovery")
        recovery.activate(self.cfg, self.key, contract)
        recovery.emit_event(self.cfg, {"id": "defect-1", "affected_tickets": [{"ticket": self.key, "recovery_id": contract["id"]}]}, "landed", "fix-1")
        attempts = 0
        def retry_then_crash(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            runner.retry_task(*args, **kwargs)
            if attempts == 1:
                raise Crash("crash after task reset")
        with self.assertRaises(Crash):
            recovery.consume_events(self.cfg, retry_task=retry_then_crash, ensure_supervisor=lambda *args: None)
        recovery.consume_events(self.cfg, retry_task=retry_then_crash, ensure_supervisor=lambda *args: None)
        entries = state.load(self.tdir).attempts[f"{self.key}/001"]
        self.assertEqual(len([row for row in entries if row.get("outcome") == ladder.OPERATOR_RESET]), 1)
        self.assertEqual(len([row for row in heal.history(self.tdir) if row.get("action") == heal.OPERATOR_RESUME]), 1)

    def test_resolution_for_queued_contract_waits_until_that_contract_is_active(self):
        first = self.contract(contract_id="recovery-first")
        second = self.contract(contract_id="recovery-second")
        second["defect_ids"] = ["defect-2"]
        recovery.activate(self.cfg, self.key, first)
        recovery.queue(self.cfg, self.key, second)
        event = recovery.emit_event(self.cfg, {"id": "defect-2", "affected_tickets": [{"ticket": self.key, "recovery_id": second["id"]}]}, "landed")
        recovery.consume_events(self.cfg, retry_task=lambda *args, **kwargs: None, ensure_supervisor=lambda *args: None)
        self.assertTrue(event.exists())
        recovery.mark_ready(self.cfg, self.key, "first-resolved")
        recovery.resume_active(self.cfg, self.key, retry_task=lambda *args, **kwargs: None, ensure_supervisor=lambda *args: None)
        recovery.consume_events(self.cfg, retry_task=lambda *args, **kwargs: None, ensure_supervisor=lambda *args: None)
        self.assertIsNone(state.load(self.tdir).recovery["active"])

    def test_a_retry_for_a_task_with_no_attempts_becomes_obsolete_not_a_crash(self):
        contract = self.contract(contract_id="retry-missing")
        recovery.activate(self.cfg, self.key, contract)
        recovery.mark_ready(self.cfg, self.key, "event-1")
        def no_attempts(*args, **kwargs):
            raise SystemExit("ZIP-1 has no attempts for task 001")
        result = recovery.resume_active(self.cfg, self.key, retry_task=no_attempts, ensure_supervisor=lambda *a: None)
        self.assertEqual(result, "obsolete")
        self.assertIsNone(state.load(self.tdir).recovery["active"])

    def test_one_failing_event_row_does_not_stop_the_others(self):
        affected = [self.event_ticket("ZIP-2", "recovery-2"), self.event_ticket("ZIP-3", "recovery-3")]
        event = recovery.emit_event(self.cfg, {"id": "defect-1", "affected_tickets": affected}, "landed", "fix")
        started = []
        def ensure(key, *args):
            if key == "ZIP-2":
                raise RuntimeError("launchctl bootstrap failed")
            started.append(key)
        results = recovery.consume_events(self.cfg, retry_task=lambda *a, **k: None, ensure_supervisor=ensure)
        self.assertEqual(started, ["ZIP-3"])
        self.assertIn("ZIP-2:error", results)
        self.assertTrue(event.exists(), "the failed row stays pending")

    def test_a_contract_not_listed_on_its_defect_is_orphaned(self):
        contract = recovery.new_contract(self.key, {"state": "implement"}, {"kind": "resume-stage", "state": "implement"}, ("shared-defect",))
        path = self.cfg.state_root / "defects" / "open" / "shared-defect.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": "shared-defect", "affected_tickets": [{"ticket": "ZIP-other", "recovery_id": "recovery-x"}]}))
        self.assertTrue(recovery.orphaned(self.cfg, contract))
        path.write_text(json.dumps({"id": "shared-defect", "affected_tickets": [{"ticket": self.key, "recovery_id": contract["id"]}]}))
        self.assertFalse(recovery.orphaned(self.cfg, contract))

    def test_supervisor_failure_preserves_pending_recovery_for_replay(self):
        affected = [self.event_ticket("ZIP-2", "recovery-2")]
        recovery.emit_event(self.cfg, {"id": "defect-1", "affected_tickets": affected}, "landed", "fix-1")
        def unavailable(*args):
            raise RuntimeError("launchd unavailable")
        first = recovery.consume_events(self.cfg, retry_task=lambda *args, **kwargs: None, ensure_supervisor=unavailable)
        self.assertEqual(first, ["ZIP-2:error"])
        started = []
        recovery.consume_events(self.cfg, retry_task=lambda *args, **kwargs: None, ensure_supervisor=lambda key, *args: started.append(key))
        self.assertEqual(started, ["ZIP-2"])

    def test_fingerprint_mismatch_is_obsolete_without_any_resume(self):
        affected = [self.event_ticket("ZIP-2", "recovery-2")]
        affected[0]["contract_fingerprint"] = "stale-fingerprint"
        recovery.emit_event(self.cfg, {"id": "defect-1", "affected_tickets": affected}, "landed", "fix-1")
        results = recovery.consume_events(self.cfg, retry_task=lambda *args, **kwargs: None, ensure_supervisor=lambda *args: self.fail("stale contract resumed"))
        self.assertEqual(results, ["ZIP-2:obsolete"])

    def test_event_for_an_obsolete_contract_is_an_audited_no_op(self):
        affected = [self.event_ticket("ZIP-2", "recovery-old")]
        ticket = state.load(self.cfg.ticket_dir("ZIP-2"))
        ticket.recovery["active"] = recovery.new_contract(
            "ZIP-2",
            {"state": "gates", "head_sha": "new"},
            {"kind": "resume-stage", "state": "gates"},
            ("defect-2",),
            contract_id="recovery-new",
        )
        state.save(self.cfg.ticket_dir("ZIP-2"), ticket)
        recovery.emit_event(self.cfg, {"id": "defect-1", "affected_tickets": affected}, "landed", "abc123")
        results = recovery.consume_events(
            self.cfg,
            retry_task=lambda *args, **kwargs: self.fail("obsolete event retried work"),
            ensure_supervisor=lambda *args: self.fail("obsolete event started supervisor"),
        )
        self.assertEqual(results, ["ZIP-2:obsolete"])
        self.assertEqual(state.load(self.cfg.ticket_dir("ZIP-2")).recovery["active"]["id"], "recovery-new")


if __name__ == "__main__":
    unittest.main()
