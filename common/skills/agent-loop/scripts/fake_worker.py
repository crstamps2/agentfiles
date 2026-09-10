"""Scenario-driven stand-in for `pi -p` used by the runner tests and `dry-run`.
Invoked as: fake_worker.py <ignored pi args...>; reads AL_SCENARIO and AL_TASK_DIR; cwd = worktree."""
import os, pathlib, sys, time, tomllib

scenario = os.environ.get("AL_SCENARIO", "pass")
task_dir = pathlib.Path(os.environ["AL_TASK_DIR"])
wt = pathlib.Path.cwd()

# The runner writes task.toml (the single task) next to task.md for machine reading.
task = tomllib.loads((task_dir / "task.toml").read_text())
first_allowed = task["allowed_files"][0].split("*")[0].rstrip("/")
target = wt / (first_allowed if first_allowed and not (wt / first_allowed).is_dir() else f"{first_allowed}/worker_touch.rb")
target.parent.mkdir(parents=True, exist_ok=True)


def result(status, reason="none", files=None, nxt="none"):
    (task_dir / "result.md").write_text(
        f"STATUS: {status}\nREASON: {reason}\nBASE: fake\nFILES: {', '.join(files or [])}\n"
        f"EVIDENCE: fake worker scenario={scenario}\nUNVERIFIED: none\nNEXT: {nxt}\n")


if scenario == "timeout":
    time.sleep(30)
    sys.exit(0)
if scenario == "owner":
    result("blocked", "owner", [], "Is the close control part of the header slot?"); sys.exit(0)
if scenario == "env":
    result("fail", "environment", [], "dev DB unreachable"); sys.exit(0)
if scenario == "pass_on_feedback" and not (task_dir / "feedback.md").exists():
    target.write_text("wrong\n"); result("fail", "test", [str(target.relative_to(wt))], "see failing assertion"); sys.exit(0)

target.write_text(f"edited by fake worker ({scenario})\n")
rel = str(target.relative_to(wt))
if scenario == "escape":
    (wt / "bin").mkdir(exist_ok=True); (wt / "bin" / "oops").write_text("x\n"); result("pass", files=[rel, "bin/oops"])
elif scenario == "tests":
    (wt / "test").mkdir(exist_ok=True); (wt / "test" / "x_test.rb").write_text("assert true\n"); result("pass", files=[rel, "test/x_test.rb"])
elif scenario == "malformed":
    (task_dir / "result.md").write_text("I did the thing.\nFILES: " + rel + "\n")
elif scenario == "fail":
    result("fail", "test", [rel], "assertion mismatch")
else:
    result("pass", files=[rel])
