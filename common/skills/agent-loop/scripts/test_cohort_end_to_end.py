"""Ten tickets through a four-slot cohort with injected failures, using the real controller modules.

Only the outside world is faked: the Jira board, cmux spinup, launchd, GitHub PR state, the repair
agent's code change, and the per-tick progress a supervised run-once would make.
"""
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import cohort
import config
import decision
import drip
import heal
import providers
import recovery
import repair
import state

KEYS = [f"ZIP-{n}" for n in range(1, 11)]
FORWARD = ("queued", "spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready", "bot-loop", "human-gate-1")


class Launchd:
    def __init__(self):
        self.running = set()

    def ensure_running(self, key, worktree, state_root):
        tdir = pathlib.Path(state_root) / "tickets" / key
        if key in self.running or (tdir / "HUMAN").exists() or (tdir / "PARKED").exists():
            return False
        self.running.add(key)
        return True

    def start(self, key, worktree, state_root):
        self.ensure_running(key, worktree, state_root)

    def stop(self, key):
        self.running.discard(key)

    def status(self, key):
        return "state=running" if key in self.running else "not loaded"


class TenTicketSimulation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        text = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
        text = text.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{self.root}/state"')
        text = text.replace('pi_agents_dir = "~/.pi/agent/agents"', f'pi_agents_dir = "{self.root}/agents"')
        (self.root / "hopper.toml").write_text(text)
        self.cfg = config.load(self.root / "hopper.toml")
        self.cfg.ensure_dirs()
        cohort.initialize(self.cfg)
        self.launchd = Launchd()
        self.tick = 0
        self.spinup_failures = {"ZIP-3"}
        board = [{"key": k, "summary": k, "status": "Selected for Work", "assignee": None} for k in KEYS]
        for p in (patch.object(drip, "board", return_value=board), patch.object(drip, "done_in_jira", return_value=set()),
                  patch.object(drip, "shared_services_ready", return_value=True),
                  patch.object(drip, "spinup_with_panes", side_effect=self.spinup),
                  patch.object(drip, "retry_spinup", side_effect=lambda cfg, t, log=print: self.spinup(t.key)),
                  patch.object(drip.time, "time", side_effect=lambda: 1_800_000_000 + 60 * self.tick)):   # one reconcile per minute
            p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def spinup(self, key, log=print):
        wt = self.root / "wts" / key.lower()
        (wt / "planning" / key.lower()).mkdir(parents=True, exist_ok=True)
        if key in self.spinup_failures:
            self.spinup_failures.discard(key)
            raise drip.SpinupFailed("setup-failed", str(wt))
        return wt

    def reconcile(self):
        report = cohort.reconcile(self.cfg, supervisor=self.launchd, pr_lookup=lambda pr: "ready", head_lookup=lambda wt: "head")
        self.assertEqual(report.errors, [])
        members = cohort.members(self.cfg)
        self.assertLessEqual(len(members), 4)
        self.assertEqual(len(members), len(set(members)))
        self.assertLessEqual(len(self.launchd.running), 7)
        for key in self.launchd.running:
            self.assertIn(key, members, f"{key} supervised outside the cohort")
        return report

    def fail(self, key, *, resume, checkpoint=None, summary=None, model=None, status=None, attempts=None):
        """A failure boundary: record the defect, then activate its contract (production order)."""
        tdir = self.cfg.ticket_dir(key)
        t = state.load(tdir)
        if attempts:
            t.attempts.update(attempts); state.save(tdir, t)
        checkpoint = dict(checkpoint or {"state": resume["state"]})
        if model:
            checkpoint["model"] = model
        defects = (heal.signature(f"sim|{summary}"),) if summary else ()
        contract = recovery.new_contract(key, checkpoint, resume, defects, status=status)
        if summary:
            repair.record_defect(self.cfg, key, "sim", summary, "evidence",
                                 recovery_ref={"ticket": key, "recovery_id": contract["id"],
                                               "contract_fingerprint": recovery.fingerprint(contract)})
        recovery.activate(self.cfg, key, contract)
        t = state.load(tdir)
        t = state.transition(t, "blocked", reason=summary or "provider exhausted")
        state.save(tdir, t)

    def repair_pass(self):
        """The repair agent lands every queued defect (its gates are tested in test_repair)."""
        for rec in repair.reconcile_queue(self.cfg):
            (self.cfg.state_root / "defects" / "landed" / f"{rec['id']}-1.json").write_text(json.dumps({**rec, "sha": "fix"}))
            (self.cfg.state_root / "defects" / "open" / f"{rec['id']}.json").unlink()
            recovery.emit_event(self.cfg, rec, "landed", "fix")

    def advance(self, key, injected):
        tdir = self.cfg.ticket_dir(key)
        t = state.load(tdir)
        if t.state in ("blocked", "paused", "decision-required", "human-gate-1") or (tdir / "HUMAN").exists():
            return
        if isinstance(t.recovery, dict) and t.recovery.get("active"):
            return
        nxt = FORWARD[FORWARD.index(t.state) + 1]
        trigger = injected.pop((key, nxt), None)
        t = state.transition(t, nxt)
        if nxt == "human-gate-1":
            t.evidence_sha = "head"
            (tdir / "ci.json").write_text(json.dumps({"pr": 1000 + KEYS.index(key), "ready_marked_sha": "head"}))
        state.save(tdir, t)
        if trigger:
            trigger(key)

    def test_ten_tickets_reach_human_gate_one_without_exceeding_four_slots(self):
        shared = "verification dirties yarn.lock outside the task allowlist"
        injected = {
            ("ZIP-1", "implement"): lambda k: self.fail(k, resume={"kind": "retry-task", "state": "implement", "task_id": "001", "task_slug": "a"},
                                                        checkpoint={"state": "implement", "task_id": "001", "task_slug": "a"},
                                                        summary="worker rejection: runner misread result.md",
                                                        attempts={f"{k}/001": [{"n": 1, "outcome": "rejected", "slug": "a"}]}),
            ("ZIP-2", "gates"): lambda k: self.fail(k, resume={"kind": "resume-stage", "state": "gates"}, summary="gate parser crashed on screenshot table"),
            ("ZIP-1", "plan"): lambda k: self.fail(k, resume={"kind": "replan", "state": "plan"}, summary=shared),
            ("ZIP-2", "plan"): lambda k: self.fail(k, resume={"kind": "replan", "state": "plan"}, summary=shared),
            ("ZIP-6", "draft-pr"): lambda k: self.fail(k, resume={"kind": "republish", "state": "implement"},
                                                       checkpoint={"state": "implement"}, summary="publication left a stale index.lock"),
            ("ZIP-7", "implement"): self.provider_outage,
            ("ZIP-8", "ready"): lambda k: self.fail(k, resume={"kind": "resume-stage", "state": "ready"}, summary="rebase conflict in generated todo file"),
            ("ZIP-9", "plan-review"): self.product_decision,
        }
        self.tick = 0
        self.decision_tick = None
        while self.tick < 400:
            self.tick += 1
            tick = self.tick
            if tick == 25:
                self.launchd = Launchd()                     # controller host restarted: launchd lost every job
            if tick % 3 == 0:
                self.repair_pass()                           # repair service runs on its own cadence
            if self.decision_tick and tick == self.decision_tick + 6:
                self.assertIn("ZIP-9", cohort.members(self.cfg), "a decision wait keeps its slot")
                decision.answer(self.cfg, "ZIP-9", "Expose immutable values.", ensure_supervisor=self.launchd.ensure_running)
            if tick == 40:
                providers.mark_healthy(self.cfg.state_root, "anthropic")
            self.reconcile()
            for key in sorted(self.launchd.running):
                self.advance(key, injected)
            gated = [k for k in KEYS if (self.cfg.ticket_dir(k) / "state.json").exists() and state.load(self.cfg.ticket_dir(k)).state == "human-gate-1"]
            if len(gated) == 10 and not cohort.members(self.cfg):
                break
        self.assertEqual(injected, {}, "every injected failure fired")
        self.assertEqual(len(gated), 10, f"stalled after {tick} ticks: {[(k, state.load(self.cfg.ticket_dir(k)).state) for k in KEYS if (self.cfg.ticket_dir(k) / 'state.json').exists()]}")
        released = [row["ticket"] for row in cohort.load(self.cfg).history if row.get("event") == "released"]
        self.assertEqual(sorted(released), sorted(KEYS))
        self.assertEqual(state.load(self.cfg.ticket_dir("ZIP-9")).decisions[0]["guidance"], "Expose immutable values.")
        shared_landings = [json.loads(p.read_text()) for p in (self.cfg.state_root / "defects" / "landed").glob("*.json")
                           if json.loads(p.read_text()).get("summary") == shared]
        self.assertEqual(len(shared_landings), 1, "one repair for the shared defect")
        self.assertEqual(sorted(row["ticket"] for row in shared_landings[0]["affected_tickets"]), ["ZIP-1", "ZIP-2"])

    def provider_outage(self, key):
        providers.mark_exhausted(self.cfg.state_root, "openai", "usage limit")
        providers.mark_exhausted(self.cfg.state_root, "anthropic", "usage limit")
        self.fail(key, resume={"kind": "wait-provider", "state": "implement"}, model="openai/gpt-5.6-terra")

    def product_decision(self, key):
        decision.propose(self.cfg, key, {
            "schema_version": 1, "summary": "Ticket and shipped API disagree on the public type.",
            "sources": ["Jira AC", "existing component"], "why_unresolved": "Neither source is authoritative.",
            "options": [{"id": "records", "label": "Records", "consequences": "Mutable public objects."},
                        {"id": "values", "label": "Values", "consequences": "Explicit writes."}],
            "recommendation": "values", "recommendation_reason": "Matches the shipped API.",
            "question": "Which type should the API expose?",
            "checkpoint": {"state": "plan"}, "resume": {"kind": "replan", "state": "plan"},
        })
        self.decision_tick = self.tick


if __name__ == "__main__":
    unittest.main()
