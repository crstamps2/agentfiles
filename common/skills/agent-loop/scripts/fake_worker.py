"""Scenario-driven stand-in for `pi -p` used by the runner tests and `dry-run`.
Invoked as: fake_worker.py <ignored pi args...>; reads AL_SCENARIO and AL_TASK_DIR; cwd = worktree."""
import os, pathlib, sys, time, tomllib

scenario = os.environ.get("AL_SCENARIO", "pass")
task_dir = pathlib.Path(os.environ["AL_TASK_DIR"])
wt = pathlib.Path.cwd()

# The runner writes task.toml (the single task) next to task.md for machine reading.
task = tomllib.loads((task_dir / "task.toml").read_text())
first_allowed = task["allowed_files"][0]
# Ruling 6: derive both the directory prefix and the extension from the glob itself, so a
# glob like "docs/**/*.md" produces a file under docs/ with a .md extension instead of always
# writing worker_touch.rb (which can land outside the allowlist).
# For literal allowlist entries (no "*"), target the file directly.
if "*" not in first_allowed:
    target = wt / first_allowed
else:
    _prefix = first_allowed.split("*")[0].rstrip("/")
    _tail = first_allowed.rsplit("*", 1)[-1]
    _ext = _tail if _tail.startswith(".") and "/" not in _tail else (pathlib.Path(_tail).suffix or ".rb")
    target = (wt / _prefix / f"worker_touch{_ext}") if _prefix else (wt / f"worker_touch{_ext}")
target.parent.mkdir(parents=True, exist_ok=True)


def result(status, reason="none", files=None, nxt="none"):
    (task_dir / "result.md").write_text(
        f"STATUS: {status}\nREASON: {reason}\nBASE: fake\nFILES: {', '.join(files or [])}\n"
        f"EVIDENCE: fake worker scenario={scenario}\nUNVERIFIED: none\nNEXT: {nxt}\n")


if scenario == "timeout":
    target.write_text(f"edited by fake worker ({scenario})\n")
    time.sleep(30)
    sys.exit(0)
if scenario == "transport_429":
    print("HTTP 429 Too Many Requests", file=sys.stderr)
    sys.exit(1)
if scenario == "owner":
    result("blocked", "owner", [], "Is the close control part of the header slot?"); sys.exit(0)
if scenario == "env":
    result("fail", "environment", [], "dev DB unreachable"); sys.exit(0)
if scenario == "env_partial":
    target.write_text(f"edited by fake worker ({scenario})\n")
    result("fail", "environment", [str(target.relative_to(wt))], "dev DB unreachable"); sys.exit(0)
if scenario == "pass_on_feedback" and not (task_dir / "feedback.md").exists():
    target.write_text("wrong\n"); result("fail", "test", [str(target.relative_to(wt))], "see failing assertion"); sys.exit(0)

target.write_text(f"edited by fake worker ({scenario})\n")
rel = str(target.relative_to(wt))
if scenario == "escape":
    (wt / "bin").mkdir(exist_ok=True); (wt / "bin" / "oops").write_text("x\n"); result("pass", files=[rel, "bin/oops"])
elif scenario == "env_escape":
    (wt / "bin").mkdir(exist_ok=True); (wt / "bin" / "oops").write_text("x\n"); result("fail", "environment", [rel, "bin/oops"], "network unavailable")
elif scenario == "tests":
    (wt / "test").mkdir(exist_ok=True); (wt / "test" / "x_test.rb").write_text("assert true\n"); result("pass", files=[rel, "test/x_test.rb"])
elif scenario == "malformed":
    (task_dir / "result.md").write_text("I did the thing.\nFILES: " + rel + "\n")
elif scenario == "fail":
    result("fail", "test", [rel], "assertion mismatch")
elif scenario == "plant_verify_symlink":
    result("pass", files=[rel])
    os.symlink(os.environ["AL_OUTSIDE"], task_dir / "verify-0.out")
else:
    result("pass", files=[rel])
