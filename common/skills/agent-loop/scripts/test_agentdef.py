# test_agentdef.py
import pathlib, tempfile, unittest
import agentdef

WORKER = """---
name: cloud-worker
description: Cheap cloud worker.
model: ollama-cloud/deepseek-v4-flash
thinking: medium
tools: read, grep, find, ls, bash, edit, write
---

You are a worker. Body line two.
"""
UNSAFE_FALLBACK = WORKER.replace("tools:", "fallbackModels: anthropic/claude-sonnet-5\ntools:")
UNSAFE_TOOLS = WORKER.replace("edit, write", "edit, write, web_search")

class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = pathlib.Path(self.tmp.name)
        (self.d/"cloud-worker.md").write_text(WORKER)
    def tearDown(self):
        self.tmp.cleanup()

    def test_load_parses_frontmatter_and_body(self):
        a = agentdef.load(self.d, "cloud-worker")
        self.assertEqual(a.model, "ollama-cloud/deepseek-v4-flash"); self.assertEqual(a.thinking, "medium")
        self.assertEqual(a.tools, ["read", "grep", "find", "ls", "bash", "edit", "write"])
        self.assertTrue(a.body.startswith("You are a worker.")); self.assertEqual(a.fallback_models, [])
    def test_missing_agent_raises(self):
        with self.assertRaises(agentdef.AgentDefError):
            agentdef.load(self.d, "nope")
    def test_worker_safe_ok(self):
        agentdef.assert_worker_safe(agentdef.load(self.d, "cloud-worker"))
    def test_worker_with_fallback_rejected(self):
        (self.d/"bad.md").write_text(UNSAFE_FALLBACK)
        with self.assertRaisesRegex(agentdef.AgentDefError, "fallbackModels"):
            agentdef.assert_worker_safe(agentdef.load(self.d, "bad"))
    def test_worker_with_extra_tool_rejected(self):
        (self.d/"bad.md").write_text(UNSAFE_TOOLS)
        with self.assertRaisesRegex(agentdef.AgentDefError, "web_search"):
            agentdef.assert_worker_safe(agentdef.load(self.d, "bad"))
    def test_pi_argv_shape(self):
        a = agentdef.load(self.d, "cloud-worker")
        argv = agentdef.pi_argv(a, self.d/"prompt.md", self.d/"sess", self.d/"body.md")
        self.assertEqual(argv[:2], ["pi", "-p"])
        self.assertIn("--model", argv); self.assertEqual(argv[argv.index("--model")+1], "ollama-cloud/deepseek-v4-flash:medium")
        self.assertEqual(argv[argv.index("--tools")+1], "read,grep,find,ls,bash,edit,write")
        self.assertEqual(argv[argv.index("--append-system-prompt")+1], str(self.d/"body.md"))
        self.assertEqual(argv[argv.index("--session-dir")+1], str(self.d/"sess"))
        self.assertEqual(argv[-1], f"@{self.d/'prompt.md'}")

if __name__ == "__main__":
    unittest.main()
