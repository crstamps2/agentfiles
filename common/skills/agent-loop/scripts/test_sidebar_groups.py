import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import config
import sidebar
import state


class GroupCmux:
    def __init__(self, destination=True):
        self.calls = []
        self.identities = {}
        self.workspaces = "workspace:11 zip-1\nworkspace:12 zip-2\n"
        self.groups = [self.group("workspace_group:1", "In Progress", ["workspace:98", "workspace:11", "workspace:12"])]
        self.groups[0]["anchor_workspace_ref"] = "workspace:98"
        if destination:
            self.groups.append(self.group("workspace_group:2", "Ready for Review", []))
        self.fail_add = False
        self.fail_list = False

    @staticmethod
    def group(ref, name, members):
        return {"ref": ref, "name": name, "anchor_workspace_ref": "workspace:99",
                "member_workspace_refs": members, "member_count": len(members),
                "member_workspace_ids": [m.replace("workspace:", "workspace-id-") for m in members]}

    def __call__(self, argv, **kwargs):
        args = argv[1:]
        self.calls.append(args)
        out = "OK\n"
        code = 0
        if args == ["workspace", "list"]:
            out = self.workspaces
        elif args == ["workspace-group", "list", "--json", "--id-format", "both"]:
            if self.fail_list:
                return subprocess.CompletedProcess(argv, 1, "", "socket unavailable")
            for group in self.groups:
                group["member_workspace_ids"] = [
                    self.identities.setdefault(ref, ref.replace("workspace:", "workspace-id-"))
                    for ref in group["member_workspace_refs"]
                ]
            out = json.dumps({"groups": self.groups, "window_ref": "window:1"})
        elif args[:2] == ["workspace-group", "create"]:
            group = self.group("workspace_group:2", "Ready for Review", ["workspace:99"])
            self.groups.append(group)
            out = json.dumps({"group": group})
        elif args[:2] == ["workspace-group", "add"]:
            if self.fail_add:
                code = 1
            else:
                ref = args[args.index("--workspace") + 1]
                target = args[args.index("--group") + 1]
                for group in self.groups:
                    group["member_workspace_refs"] = [m for m in group["member_workspace_refs"] if m != ref]
                    if group["ref"] == target:
                        group["member_workspace_refs"].append(ref)
        if code and kwargs.get("check"):
            raise subprocess.CalledProcessError(code, argv, out, "cmux failed")
        return subprocess.CompletedProcess(argv, code, out, "")

    def members(self, name):
        return next(g["member_workspace_refs"] for g in self.groups if g["name"] == name)


class ReviewGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        toml = (pathlib.Path(__file__).resolve().parent.parent / "hopper.toml").read_text()
        toml = toml.replace('state_root = "~/.local/state/agent-loop"', f'state_root = "{self.root}/state"')
        (self.root / "hopper.toml").write_text(toml)
        self.cfg = config.load(self.root / "hopper.toml")
        self.cfg.ensure_dirs()
        self.cmux = GroupCmux()
        self.addCleanup(patch.stopall)
        patch("sidebar.PRODUCTION_ROOT", self.cfg.state_root).start()
        patch("watch.pr_state", return_value="ready").start()
        patch("cohort._head", return_value="verified-head").start()

    def ticket(self, key="ZIP-1", stage="human-gate-1", **extra):
        ticket = state.Ticket(key=key, state=stage, worktree=str(self.root),
                              evidence_sha="verified-head", **extra)
        state.save(self.cfg.ticket_dir(key), ticket)
        (self.cfg.ticket_dir(key) / "ci.json").write_text(json.dumps({"pr": 123}))
        return ticket

    def sync(self):
        sidebar.sync(self.cfg, run=self.cmux, force=True)

    def test_verified_gate_moves_only_the_ready_ticket(self):
        self.ticket()
        self.ticket("ZIP-2", "bot-loop")
        self.sync()
        self.assertEqual(self.cmux.members("Ready for Review"), ["workspace:11"])
        self.assertEqual(self.cmux.members("In Progress"), ["workspace:98", "workspace:12"])

    def test_group_routing_is_not_skipped_by_the_status_pill_cache(self):
        self.ticket()
        self.cmux.fail_list = True
        self.sync()
        self.assertTrue((self.cfg.state_root / "sidebar.json").exists())
        self.cmux.fail_list = False
        self.sync()
        self.assertEqual(self.cmux.members("Ready for Review"), ["workspace:11"])

    def test_manual_review_move_is_never_reversed(self):
        self.ticket()
        self.sync()
        self.assertEqual(self.cmux.members("Ready for Review"), ["workspace:11"])
        self.cmux.groups.append(GroupCmux.group("workspace_group:3", "In Review", ["workspace:11"]))
        self.cmux.members("Ready for Review").remove("workspace:11")
        self.sync()
        self.assertEqual(self.cmux.members("In Review"), ["workspace:11"])
        self.assertEqual(self.cmux.members("Ready for Review"), [])

    def test_only_in_progress_is_an_automatic_source(self):
        self.ticket()
        for source in ("In Review", "Done", "Approved", "On Hold", "Archive"):
            with self.subTest(source=source):
                self.cmux = GroupCmux()
                self.cmux.groups[0]["name"] = source
                self.sync()
                self.assertEqual(self.cmux.members(source), ["workspace:98", "workspace:11", "workspace:12"])
                self.assertEqual(self.cmux.members("Ready for Review"), [])

    def test_missing_destination_is_created_without_capturing_the_focused_workspace(self):
        self.ticket()
        self.cmux = GroupCmux(destination=False)
        self.sync()
        self.sync()
        self.assertEqual(sum(g["name"] == "Ready for Review" for g in self.cmux.groups), 1)
        self.assertEqual(self.cmux.members("Ready for Review"), ["workspace:99", "workspace:11"])
        creates = [c for c in self.cmux.calls if c[:2] == ["workspace-group", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertIn("--idempotency-key", creates[0])
        self.assertNotIn("--from", creates[0])

    def test_failed_move_retries_on_the_next_sync(self):
        self.ticket()
        self.cmux.fail_add = True
        self.sync()
        self.assertEqual(self.cmux.members("Ready for Review"), [])
        self.cmux.fail_add = False
        self.sync()
        self.assertEqual(self.cmux.members("Ready for Review"), ["workspace:11"])

    def test_unverified_gate_never_moves_or_creates_a_group(self):
        for stage in ("draft-pr", "ready", "bot-loop", "colleague-loop", "human-gate-2", "done"):
            with self.subTest(stage=stage):
                self.cmux = GroupCmux(destination=False)
                self.ticket(stage=stage)
                self.sync()
                self.assertEqual(self.cmux.members("In Progress"), ["workspace:98", "workspace:11", "workspace:12"])
                self.assertFalse(any(c[:2] == ["workspace-group", "create"] for c in self.cmux.calls))

    def test_stale_sha_draft_closed_or_merged_pr_never_moves(self):
        self.ticket()
        for pr_status, head in (("draft", "verified-head"), ("closed", "verified-head"),
                                ("merged", "verified-head"), ("ready", "new-head")):
            with self.subTest(pr_status=pr_status, head=head):
                self.cmux = GroupCmux()
                with patch("watch.pr_state", return_value=pr_status), patch("cohort._head", return_value=head):
                    self.sync()
                self.assertEqual(self.cmux.members("Ready for Review"), [])

    def test_recovery_or_open_decision_never_moves(self):
        for extra in ({"recovery": {"active": {"status": "waiting-repair"}, "queued": []}},
                      {"recovery": {"active": None, "queued": [{"id": "pending"}]}},
                      {"decision_request": {"id": "owner-question"}}):
            with self.subTest(extra=extra):
                self.cmux = GroupCmux()
                self.ticket(**extra)
                self.sync()
                self.assertEqual(self.cmux.members("Ready for Review"), [])

    def test_missing_pr_or_worktree_never_moves(self):
        self.ticket()
        (self.cfg.ticket_dir("ZIP-1") / "ci.json").write_text("{}")
        self.sync()
        self.assertEqual(self.cmux.members("Ready for Review"), [])
        (self.cfg.ticket_dir("ZIP-1") / "ci.json").write_text('{"pr":123}')
        ticket = state.load(self.cfg.ticket_dir("ZIP-1"))
        ticket.worktree = str(self.root / "missing")
        state.save(self.cfg.ticket_dir("ZIP-1"), ticket)
        self.sync()
        self.assertEqual(self.cmux.members("Ready for Review"), [])

    def test_duplicate_workspace_names_are_not_arbitrarily_routed(self):
        self.ticket()
        self.cmux.workspaces += "workspace:21 zip-1\n"
        self.cmux.members("In Progress").append("workspace:21")
        self.sync()
        self.assertEqual(self.cmux.members("Ready for Review"), [])

    def test_ambiguous_destination_groups_do_not_move_work(self):
        self.ticket()
        self.cmux.groups.append(GroupCmux.group("workspace_group:3", "Ready for Review", []))
        self.sync()
        self.assertEqual(self.cmux.members("In Progress"), ["workspace:98", "workspace:11", "workspace:12"])

    def test_human_move_during_gate_verification_is_preserved(self):
        self.ticket()

        def move_to_review(_):
            self.cmux.members("In Progress").remove("workspace:11")
            self.cmux.groups.append(GroupCmux.group("workspace_group:3", "In Review", ["workspace:11"]))
            return "ready"

        with patch("watch.pr_state", side_effect=move_to_review):
            self.sync()
        self.assertEqual(self.cmux.members("In Review"), ["workspace:11"])
        self.assertEqual(self.cmux.members("Ready for Review"), [])

    def test_manual_return_to_in_progress_is_not_reversed(self):
        self.ticket()
        self.sync()
        self.cmux.members("Ready for Review").remove("workspace:11")
        self.cmux.members("In Progress").append("workspace:11")
        self.sync()
        self.assertIn("workspace:11", self.cmux.members("In Progress"))
        self.assertEqual(self.cmux.members("Ready for Review"), [])

    def test_new_verified_head_can_be_handed_off_again(self):
        self.ticket()
        self.sync()
        self.cmux.members("Ready for Review").remove("workspace:11")
        self.cmux.members("In Progress").append("workspace:11")
        ticket = state.load(self.cfg.ticket_dir("ZIP-1"))
        ticket.evidence_sha = "new-head"
        state.save(self.cfg.ticket_dir("ZIP-1"), ticket)
        with patch("cohort._head", return_value="new-head"):
            self.sync()
        self.assertEqual(self.cmux.members("Ready for Review"), ["workspace:11"])

    def test_manual_return_is_preserved_when_cmux_short_refs_change(self):
        self.ticket()
        self.sync()
        self.assertEqual(self.cmux.members("Ready for Review"), ["workspace:11"])
        self.cmux.members("Ready for Review").remove("workspace:11")
        self.cmux.members("In Progress").append("workspace:41")
        self.cmux.workspaces = self.cmux.workspaces.replace("workspace:11", "workspace:41")
        self.cmux.identities["workspace:41"] = "workspace-id-11"
        self.sync()
        self.assertIn("workspace:41", self.cmux.members("In Progress"))
        self.assertEqual(self.cmux.members("Ready for Review"), [])

    def test_case_variant_duplicate_names_are_not_routed(self):
        self.ticket()
        self.cmux.workspaces += "workspace:21 ZIP-1\n"
        self.cmux.members("In Progress").append("workspace:21")
        self.sync()
        self.assertEqual(self.cmux.members("Ready for Review"), [])

    def test_in_progress_anchor_is_not_routed(self):
        self.ticket()
        self.cmux.groups[0]["anchor_workspace_ref"] = "workspace:11"
        self.sync()
        self.assertEqual(self.cmux.members("Ready for Review"), [])

    def test_owned_or_parked_tickets_are_not_routed(self):
        self.ticket()
        for marker in ("HUMAN", "INTERACTIVE.json", "PARKED"):
            with self.subTest(marker=marker):
                self.cmux = GroupCmux()
                path = self.cfg.ticket_dir("ZIP-1") / marker
                path.write_text("{}")
                self.sync()
                path.unlink()
                self.assertEqual(self.cmux.members("Ready for Review"), [])

    def test_ticket_identity_must_match_the_workspace(self):
        ticket = self.ticket()
        ticket.key = "ZIP-99"
        state.save(self.cfg.ticket_dir("ZIP-1"), ticket)
        self.sync()
        self.assertEqual(self.cmux.members("Ready for Review"), [])

    def test_ticket_change_during_verification_prevents_group_creation(self):
        self.ticket()
        self.cmux = GroupCmux(destination=False)

        def revoke_gate(_):
            ticket = state.load(self.cfg.ticket_dir("ZIP-1"))
            ticket.state = "decision-required"
            state.save(self.cfg.ticket_dir("ZIP-1"), ticket)
            return "ready"

        with patch("watch.pr_state", side_effect=revoke_gate):
            self.sync()
        self.assertFalse(any(c[:2] == ["workspace-group", "create"] for c in self.cmux.calls))
        self.assertIn("workspace:11", self.cmux.members("In Progress"))

    def test_gate_revocation_while_creating_the_group_prevents_the_move(self):
        self.ticket()
        self.cmux = GroupCmux(destination=False)
        delegate = self.cmux

        def revoke_after_create(argv, **kwargs):
            result = delegate(argv, **kwargs)
            if argv[1:3] == ["workspace-group", "create"]:
                ticket = state.load(self.cfg.ticket_dir("ZIP-1"))
                ticket.state = "decision-required"
                state.save(self.cfg.ticket_dir("ZIP-1"), ticket)
            return result

        sidebar.sync(self.cfg, run=revoke_after_create, force=True)
        self.assertIn("workspace:11", self.cmux.members("In Progress"))
        self.assertFalse(any(c[:2] == ["workspace-group", "add"] for c in self.cmux.calls))

    def test_human_move_while_creating_the_group_is_preserved(self):
        self.ticket()
        self.cmux = GroupCmux(destination=False)
        delegate = self.cmux

        def move_after_create(argv, **kwargs):
            result = delegate(argv, **kwargs)
            if argv[1:3] == ["workspace-group", "create"]:
                self.cmux.members("In Progress").remove("workspace:11")
                self.cmux.groups.append(GroupCmux.group("workspace_group:3", "In Review", ["workspace:11"]))
            return result

        sidebar.sync(self.cfg, run=move_after_create)
        self.assertEqual(self.cmux.members("In Review"), ["workspace:11"])
        self.assertFalse(any(c[:2] == ["workspace-group", "add"] for c in self.cmux.calls))

    def test_workspace_identity_change_while_creating_the_group_is_not_routed(self):
        self.ticket()
        self.cmux = GroupCmux(destination=False)
        delegate = self.cmux

        def replace_after_create(argv, **kwargs):
            result = delegate(argv, **kwargs)
            if argv[1:3] == ["workspace-group", "create"]:
                self.cmux.identities["workspace:11"] = "replacement-workspace-id"
            return result

        sidebar.sync(self.cfg, run=replace_after_create)
        self.assertIn("workspace:11", self.cmux.members("In Progress"))
        self.assertFalse(any(c[:2] == ["workspace-group", "add"] for c in self.cmux.calls))

    def test_custom_state_root_has_no_cmux_or_github_side_effects(self):
        self.ticket()
        with patch("sidebar.PRODUCTION_ROOT", self.root / "not-production"), patch("watch.pr_state") as pr:
            sidebar.sync(self.cfg, run=self.cmux)
        self.assertEqual(self.cmux.calls, [])
        pr.assert_not_called()

    def test_force_cannot_authorize_custom_root_side_effects(self):
        self.ticket()
        with patch("sidebar.PRODUCTION_ROOT", self.root / "not-production"), patch("watch.pr_state") as pr:
            sidebar.sync(self.cfg, run=self.cmux, force=True)
        self.assertEqual(self.cmux.calls, [])
        pr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
