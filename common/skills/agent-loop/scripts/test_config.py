# test_config.py
import os, pathlib, tempfile, unittest
import config

MINI = """
[hopper]
epic = "ZIP-1"
owned_epics = ["ZIP-1"]
[[hopper.tickets]]
key = "ZIP-10"
[[hopper.tickets]]
key = "ZIP-11"
deps = ["ZIP-10"]
pin_arm = "cloud"
[paths]
state_root = "{root}"
cmux_chain_dir = "/tmp/cc"
pi_agents_dir = "/tmp/agents"
[timeouts]
heavy_stage_s = 10
light_stage_s = 5
[admission]
compressor_pct_max = 25.0
load_per_core_max = 1.0
disk_free_gb_min = 20.0
require_ac_power = true
defer_max_s = 60
[budgets]
premium_daily_usd = 1.0
premium_monthly_usd = 2.0
total_unattended_daily_usd = 3.0
cloud_starter_credit_usd = 0.0
ci_rerun_max = 5
[protection]
protected_paths = ["bin/"]
test_path_globs = ["test/**"]
[arms]
alternate = ["cloud", "local"]
[outcomes]
primary = "p"
secondary = []
"""

class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "hopper.toml"
        self.path.write_text(MINI.format(root=self.tmp.name + "/state"))
    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_tickets_in_order_with_deps_and_pin(self):
        c = config.load(self.path)
        self.assertEqual(c.epic, "ZIP-1")
        self.assertEqual(list(c.tickets), ["ZIP-10", "ZIP-11"])
        self.assertEqual(c.tickets["ZIP-11"].deps, ["ZIP-10"])
        self.assertEqual(c.tickets["ZIP-11"].pin_arm, "cloud")
        self.assertIsNone(c.tickets["ZIP-10"].pin_arm)

    def test_paths_expand_user_and_ticket_dir(self):
        c = config.load(self.path)
        self.assertTrue(c.state_root.is_absolute())
        self.assertEqual(c.ticket_dir("ZIP-10"), c.state_root / "tickets" / "ZIP-10")

    def test_ensure_dirs_creates_layout_with_0700(self):
        c = config.load(self.path)
        c.ensure_dirs()
        for sub in ("tickets", "locks", "attempts"):
            self.assertTrue((c.state_root / sub).is_dir())
        self.assertEqual(oct(c.state_root.stat().st_mode & 0o777), "0o700")

    def test_default_path_is_hopper_toml_beside_scripts(self):
        self.assertEqual(config.DEFAULT_PATH.name, "hopper.toml")
        self.assertEqual(config.DEFAULT_PATH.parent.name, "agent-loop")

    def test_missing_required_table_raises(self):
        self.path.write_text("[hopper]\nepic='X'\n")
        with self.assertRaises(config.ConfigError):
            config.load(self.path)

    def test_local_model_must_be_ctx_pinned(self):
        self.path.write_text(MINI.format(root=self.tmp.name + "/state") + '\n[local]\nmodel = "ollama-local/gpt-oss:20b"\n')
        with self.assertRaisesRegex(config.ConfigError, "ctx"):
            config.load(self.path)
        # Only the spec's exact ctx32k pin is accepted -- ctx4k (or any other context size) is
        # still rejected even though it matches the looser "-ctx\d+k:" shape.
        self.path.write_text(MINI.format(root=self.tmp.name + "/state") + '\n[local]\nmodel = "ollama-local/gpt-oss-ctx4k:20b"\n')
        with self.assertRaisesRegex(config.ConfigError, "ctx"):
            config.load(self.path)
        self.path.write_text(MINI.format(root=self.tmp.name + "/state") + '\n[local]\nmodel = "ollama-local/gpt-oss-ctx32k:20b"\nunload_after_attempt = true\n')
        self.assertEqual(config.load(self.path).local_model, "ollama-local/gpt-oss-ctx32k:20b")

    def test_local_table_present_with_missing_or_empty_model_raises(self):
        self.path.write_text(MINI.format(root=self.tmp.name + "/state") + '\n[local]\nunload_after_attempt = true\n')
        with self.assertRaises(config.ConfigError):
            config.load(self.path)
        self.path.write_text(MINI.format(root=self.tmp.name + "/state") + '\n[local]\nmodel = ""\n')
        with self.assertRaises(config.ConfigError):
            config.load(self.path)

if __name__ == "__main__":
    unittest.main()
