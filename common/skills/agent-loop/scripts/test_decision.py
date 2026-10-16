import copy
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import config
import decision
import state


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = pathlib.Path(self.tmp.name)
        toml = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text().replace("\ncapacity = 2 ", "\ncapacity = 4 ")
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{root}/state"')
        toml = toml.replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{root}"')
        (root / "hopper.toml").write_text(toml)
        self.cfg = config.load(root / "hopper.toml"); self.cfg.ensure_dirs()
        self.key = "ZIP-1"; self.wt = root / "wt"; (self.wt / "planning" / "zip-1").mkdir(parents=True)
        state.save(self.cfg.ticket_dir(self.key), state.Ticket(key=self.key, state="implement", previous="plan-review", worktree=str(self.wt)))

    def tearDown(self):
        self.tmp.cleanup()

    def request(self):
        return {
            "schema_version": 1,
            "summary": "The public API can expose either records or immutable values.",
            "sources": ["Jira acceptance criterion 2", "existing component API"],
            "options": [
                {"id": "records", "label": "Expose records", "consequences": "Callers can mutate persisted objects."},
                {"id": "values", "label": "Expose values", "consequences": "Callers need an explicit save command."},
            ],
            "recommendation": "values",
            "recommendation_reason": "Explicit writes preserve the existing command boundary.",
            "why_unresolved": "The ticket asks for records while the shipped API documents values.",
            "question": "Which API boundary should this component expose?",
            "checkpoint": {"state": "implement", "head_sha": "abc123"},
            "resume": {"kind": "resume-stage", "state": "implement"},
        }

    def test_schema_rejects_missing_or_ambiguous_fields(self):
        mutations = {
            "sources": lambda p: p.update(sources=[]),
            "options": lambda p: p.update(options=p["options"][:1]),
            "different options": lambda p: p["options"].__setitem__(1, dict(p["options"][0])),
            "consequences": lambda p: p["options"][0].update(consequences=""),
            "recommendation": lambda p: p.update(recommendation="other"),
            "question": lambda p: p.update(question=""),
            "checkpoint": lambda p: p.update(checkpoint={}),
            "resume": lambda p: p.update(resume={"kind": "retry-task", "state": "implement"}),
            "why unresolved": lambda p: p.update(why_unresolved=""),
            "recommendation reasoning": lambda p: p.update(recommendation_reason=""),
            "oversize": lambda p: p.update(summary="x" * 40000),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                payload = self.request(); mutate(payload)
                with self.assertRaises(decision.DecisionError):
                    decision.validate_request(payload)

    def test_documented_decision_payload_is_accepted(self):
        payload = {
            "schema_version": 1,
            "id": "decision-example",
            "status": "open",
            "ambiguity": "The ticket and shipped API use different public names.",
            "sources_checked": ["Jira ticket", "current component"],
            "why_unresolved": "Neither source authorizes a compatibility break.",
            "options": [
                {"id": "A", "choice": "Keep the existing name", "consequence": "Preserves compatibility."},
                {"id": "B", "choice": "Use the new name", "consequence": "Requires a migration."},
            ],
            "recommendation": "A, because there is no approved migration.",
            "question": "Which public name should this expose?",
            "checkpoint": {"state": "plan", "head_sha": "abc123", "resume": {"kind": "replan", "state": "plan"}},
        }
        ticket = decision.propose(self.cfg, self.key, payload)
        self.assertEqual(ticket.state, "decision-required")
        self.assertEqual(ticket.decision_request["resume"], {"kind": "replan", "state": "plan"})

    def test_future_schema_refusal_does_not_mutate_input_or_ticket(self):
        payload = self.request(); payload["schema_version"] = 2; before = copy.deepcopy(payload)
        with self.assertRaises(decision.DecisionError):
            decision.propose(self.cfg, self.key, payload)
        self.assertEqual(payload, before)
        self.assertEqual(state.load(self.cfg.ticket_dir(self.key)).state, "implement")

    def test_propose_enters_decision_required_and_is_idempotent(self):
        notices = []
        first = decision.propose(self.cfg, self.key, self.request(), notify=lambda title, body: notices.append((title, body)))
        second = decision.propose(self.cfg, self.key, self.request(), notify=lambda *args: notices.append(args))
        self.assertEqual(first.state, "decision-required")
        self.assertEqual(first.decision_request["id"], second.decision_request["id"])
        self.assertEqual(len(notices), 1)

    def test_answer_persists_history_before_clear_then_resumes_saved_stage(self):
        decision.propose(self.cfg, self.key, self.request())
        seen = []
        real_save = state.save

        def observe(path, ticket):
            seen.append((len(ticket.decisions), bool(ticket.decision_request), ticket.state))
            return real_save(path, ticket)

        supervisors = []
        with patch.object(decision.state, "save", side_effect=observe):
            answered = decision.answer(self.cfg, self.key, "Use immutable values.", ensure_supervisor=lambda *args: supervisors.append(args))
        self.assertIn((1, True, "decision-required"), seen)
        self.assertEqual(answered.state, "implement")
        self.assertEqual(answered.decision_request, {})
        self.assertEqual(answered.decisions[0]["guidance"], "Use immutable values.")
        self.assertEqual(len(supervisors), 1)
        plan = self.wt / "planning" / "zip-1" / "plan.md"
        transcript = self.wt / "planning" / "zip-1" / "transcript.md"
        self.assertIn("Use immutable values.", plan.read_text())
        self.assertIn("Use immutable values.", transcript.read_text())

    def test_replan_resume_uses_plan_boundary(self):
        payload = self.request(); payload["resume"] = {"kind": "replan", "state": "plan"}
        decision.propose(self.cfg, self.key, payload)
        answered = decision.answer(self.cfg, self.key, "The boundary changed; replan.", ensure_supervisor=lambda *args: None)
        self.assertEqual(answered.state, "plan")

    def test_open_decision_retains_its_cohort_slot(self):
        import cohort
        cohort.initialize(self.cfg)
        cohort.reserve(self.cfg, self.key, "test")
        cohort.activate(self.cfg, self.key)
        decision.propose(self.cfg, self.key, self.request())
        self.assertEqual(cohort.members(self.cfg), ["ZIP-1"])
        self.assertEqual(cohort.available(self.cfg), 3)

    def test_propose_persists_before_notifying(self):
        observed = []
        decision.propose(self.cfg, self.key, self.request(), notify=lambda *args: observed.append(state.load(self.cfg.ticket_dir(self.key)).state))
        self.assertEqual(observed, ["decision-required"])

    def test_replay_after_context_failure_keeps_one_immutable_answer(self):
        decision.propose(self.cfg, self.key, self.request())
        with patch.object(decision, "_append_context", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                decision.answer(self.cfg, self.key, "Use values.", ensure_supervisor=lambda *args: None)
        answered = decision.answer(self.cfg, self.key, "Use values.", ensure_supervisor=lambda *args: None)
        self.assertEqual(len(answered.decisions), 1)
        self.assertEqual(answered.decisions[0]["guidance"], "Use values.")

    def test_answer_rejects_empty_guidance_or_no_open_request_without_mutation(self):
        before = copy.deepcopy(vars(state.load(self.cfg.ticket_dir(self.key))))
        for guidance in ("", "  ", "Use values."):
            with self.assertRaises(decision.DecisionError):
                decision.answer(self.cfg, self.key, guidance, ensure_supervisor=lambda *args: None)
        self.assertEqual(vars(state.load(self.cfg.ticket_dir(self.key))), before)


if __name__ == "__main__":
    unittest.main()
