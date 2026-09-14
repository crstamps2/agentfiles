"""CI babysitting: observe a PR's checks, classify the result, decide the ONE next action.

The runner (not a model) decides. Shapes learned from PR #47888 (2026-09-12):
  * GitHub Actions checks: Translations, Dependent Issues, RuboCop, Reek, Frontend Linters,
    Enforce Code Freeze -- `bucket` in {pass, fail, pending, skipping, cancel}.
  * `claude-review` / `claude-security-review` are SKIPPED on draft PRs and run only once the
    PR is marked ready. The bot-review loop therefore sits after mark-ready, never before.
  * CircleCI `zipline` (the Rails suite) reports through the commit status API with a
    circleci.com link; it is the slow, flaky one.

Decision table (design §CI):
  all pass                  -> "green"
  any pending               -> "wait"
  fail & behind base        -> "rebase"      (rebase first; a stale base is the cheapest cause)
  fail & infra-looking      -> "rerun"       (only via the provider's rerun API; never an empty commit)
  fail & code-looking       -> "fix"         (a scoped task for the ladder, with the failure excerpt)
  attempts >= max           -> "escalate"    (ticket paused with reason; Cody decides)
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field

MAX_CI_ATTEMPTS = 5

INFRA_PATTERNS = (
    r"\b(ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b", r"rate limit", r"429 Too Many", r"\b50[234]\b",
    r"Could not resolve host", r"context deadline exceeded", r"exceeded the maximum time",
    r"timed out waiting", r"No space left on device", r"runner .* lost communication",
    r"executor .* failed to start", r"Docker image .* pull", r"cache restore failed", r"bundler: failed to load",
    r"The operation was canceled", r"lost connection to the ssh agent", r"ResourceExhausted",
)
FLAKY_HINTS = (r"Capybara::ElementNotFound", r"Net::ReadTimeout", r"Selenium::WebDriver::Error", r"deadlock detected",
               r"PG::ConnectionBad", r"Redis::CannotConnectError", r"Elasticsearch::Transport::Transport::Errors")


@dataclass
class Check:
    name: str
    bucket: str          # pass | fail | pending | skipping | cancel
    link: str = ""
    workflow: str = ""


@dataclass
class Verdict:
    action: str          # green | wait | rebase | rerun | fix | escalate
    reason: str
    failing: list = field(default_factory=list)
    pending: list = field(default_factory=list)


def fetch_checks(pr_number: int, repo: str, cwd) -> list[Check]:
    r = subprocess.run(["gh", "pr", "checks", str(pr_number), "--repo", repo, "--json", "name,state,bucket,link,workflow"],
                       cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    # gh exits 8 when checks are pending and 1 when some fail; the JSON is still complete.
    if not r.stdout.strip():
        raise RuntimeError(f"gh pr checks produced no output rc={r.returncode}: {r.stderr.strip()[:300]}")
    out = []
    for row in json.loads(r.stdout):
        out.append(Check(row["name"], row.get("bucket", "pending"), row.get("link") or "", row.get("workflow") or ""))
    return out


def behind_base(cwd, base: str = "main") -> int:
    subprocess.run(["git", "fetch", "-q", "origin", base], cwd=str(cwd), capture_output=True, timeout=300)
    r = subprocess.run(["git", "rev-list", "--count", f"HEAD..origin/{base}"], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace")
    return int(r.stdout.strip() or 0)


def looks_infra(text: str) -> bool:
    return any(re.search(p, text, re.I) for p in INFRA_PATTERNS)


def looks_flaky(text: str) -> bool:
    return any(re.search(p, text) for p in FLAKY_HINTS)


def classify(checks: list[Check], *, behind: int, attempts: int, failure_text: str = "",
             prior_actions: tuple = ()) -> Verdict:
    """Pure decision. `failure_text` is whatever log excerpt the caller could obtain (may be '')."""
    failing = [c.name for c in checks if c.bucket in ("fail", "cancel")]
    pending = [c.name for c in checks if c.bucket == "pending"]
    if not failing and not pending:
        return Verdict("green", "all checks passed or skipped", [], [])
    if not failing:
        return Verdict("wait", f"{len(pending)} check(s) still running", [], pending)
    if attempts >= MAX_CI_ATTEMPTS:
        return Verdict("escalate", f"CI still failing after {attempts} loop attempts: {', '.join(failing)}", failing, pending)
    if behind > 0 and "rebase" not in prior_actions[-1:]:
        return Verdict("rebase", f"{behind} commit(s) behind base; rebase before diagnosing {', '.join(failing)}", failing, pending)
    if failure_text and (looks_infra(failure_text) or looks_flaky(failure_text)):
        # never rerun twice in a row on the same head: the second identical failure is real
        if prior_actions[-1:] == ("rerun",):
            return Verdict("fix", f"infra-looking failure repeated after rerun; treating as real: {', '.join(failing)}", failing, pending)
        return Verdict("rerun", f"infra/flake signature in failure output: {', '.join(failing)}", failing, pending)
    if not failure_text:
        return Verdict("rerun" if prior_actions[-1:] != ("rerun",) else "fix",
                       f"no failure output obtainable for {', '.join(failing)}", failing, pending)
    return Verdict("fix", f"code failure in {', '.join(failing)}", failing, pending)


def gha_failure_excerpt(link: str, repo: str, cwd, max_chars: int = 6000) -> str:
    """For a github.com/.../actions/runs/<id> link, pull the failed step log tail."""
    m = re.search(r"/actions/runs/(\d+)", link or "")
    if not m:
        return ""
    r = subprocess.run(["gh", "run", "view", m.group(1), "--repo", repo, "--log-failed"],
                       cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    txt = r.stdout or r.stderr or ""
    return txt[-max_chars:]


def rerun_gha(link: str, repo: str, cwd) -> bool:
    m = re.search(r"/actions/runs/(\d+)", link or "")
    if not m:
        return False
    r = subprocess.run(["gh", "run", "rerun", m.group(1), "--repo", repo, "--failed"],
                       cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    return r.returncode == 0


def circleci_workflow_id(link: str) -> str | None:
    m = re.search(r"circleci\.com/workflow/([0-9a-f-]{36})", link or "")
    return m.group(1) if m else None
