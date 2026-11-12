import dataclasses
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import config
from test_config import MINI


class ProjectConfigTests(unittest.TestCase):
    def test_missing_local_config_explains_setup_without_using_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = pathlib.Path(tmp) / "hopper.toml"
            with patch.object(config, "DEFAULT_PATH", missing), \
                 self.assertRaisesRegex(config.ConfigError, "hopper.example.toml"):
                config.load()
            self.assertFalse(missing.exists())

    def test_project_settings_are_loaded_from_explicit_local_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "hopper.toml"
            path.write_text(MINI.split("[project]")[0].format(root=tmp) + '''
[project]
github_repo = "team/custom-app"
jira_base_url = "https://issues.example.org/browse"
operator_email = "operator@example.org"
worktrees_root = "~/worktrees/custom"
pr_footer = "Automated implementation"
house_style_example_pr = 42
''')
            cfg = config.load(path)
            self.assertEqual(cfg.github_repo, "team/custom-app")
            self.assertEqual(cfg.jira_base_url, "https://issues.example.org/browse")
            self.assertEqual(cfg.circleci_project, "gh/team/custom-app")
            self.assertEqual(cfg.operator_email, "operator@example.org")
            self.assertEqual(cfg.worktrees_root, pathlib.Path.home() / "worktrees/custom")
            self.assertEqual(cfg.house_style_example_pr, 42)

    def test_example_cannot_start_runtime_without_operator_configuration(self):
        cfg = config.load(config.EXAMPLE_PATH)
        with self.assertRaisesRegex(config.ConfigError, "configured = true"):
            config.configure_project(cfg)

    def test_example_cli_fails_before_creating_runtime_directories(self):
        import runner
        with patch.object(config.Config, "ensure_dirs") as ensure_dirs, \
             self.assertRaisesRegex(config.ConfigError, "configured = true"):
            runner.main(["--config", str(config.EXAMPLE_PATH), "status"])
        ensure_dirs.assert_not_called()

    def test_incomplete_enabled_project_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "hopper.toml"
            path.write_text(MINI.split("[project]")[0].format(root=tmp) + "\n[project]\nconfigured = true\n")
            with self.assertRaisesRegex(config.ConfigError, "requires nonempty"):
                config.load(path)

    def test_runtime_consumers_receive_configured_project(self):
        import ci
        import drip
        import publish
        cfg = config.load(config.EXAMPLE_PATH)
        cfg = dataclasses.replace(cfg, project_configured=True, github_repo="team/custom-app", circleci_project="gh/team/custom-app",
                                  jira_base_url="https://issues.example.org/browse",
                                  operator_email="operator@example.org")
        with patch.object(publish, "REPO"), patch.object(publish, "JIRA_BASE"), \
             patch.object(publish, "FOOTER"), patch.object(ci, "CIRCLE_PROJECT"), \
             patch.object(drip, "ME"), patch.object(drip, "WORKTREES"), patch.object(drip, "APP_REPO"):
            config.configure_project(cfg)
            self.assertEqual(publish.REPO, "team/custom-app")
            self.assertEqual(publish.JIRA_BASE, "https://issues.example.org/browse")
            self.assertEqual(ci.CIRCLE_PROJECT, "gh/team/custom-app")
            self.assertEqual(drip.ME, "operator@example.org")
            self.assertEqual(drip.APP_REPO, cfg.workspace_repo)


if __name__ == "__main__":
    unittest.main()
