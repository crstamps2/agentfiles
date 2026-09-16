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
               r"PG::ConnectionBad", r"Redis::CannotConnectError", r"Elasticsearch::Transport::Transport::Errors",
               r"Elastic::Transport::Transport::Errors::ServiceUnavailable", r"missing shards", r"search_phase_execution_exception",
               r"Errno::ECONNREFUSED", r"Faraday::ConnectionFailed")


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
    if failure_text and (looks_infra(failure_text) or looks_flaky(failure_text)):
        # A visible infra/flake signature outranks "behind base": a rebase cannot fix an ES 503 and
        # costs a full CI run (ZIP-4294 sat on a rebase verdict for a flaky unit_tests job).
        # Never rerun twice in a row on the same head: the second identical failure is real.
        if prior_actions[-1:] == ("rerun",):
            return Verdict("fix", f"infra-looking failure repeated after rerun; treating as real: {', '.join(failing)}", failing, pending)
        return Verdict("rerun", f"infra/flake signature in failure output: {', '.join(failing)}", failing, pending)
    if behind > 0 and "rebase" not in prior_actions[-1:]:
        return Verdict("rebase", f"{behind} commit(s) behind base; rebase before diagnosing {', '.join(failing)}", failing, pending)
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


# ----------------------------------------------------------------------------- CircleCI

CIRCLE_API = "https://circleci.com/api/v2"
CIRCLE_PROJECT = "gh/retailzipline/zipline-app"


def _circle(path: str):
    import os, urllib.request
    tok = os.environ.get("CIRCLECI_TOKEN") or _keychain("CIRCLECI_TOKEN")
    if not tok:
        return None
    req = urllib.request.Request(f"{CIRCLE_API}{path}", headers={"Circle-Token": tok})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def _keychain(service: str) -> str | None:
    r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def circleci_failure_excerpt(link: str, max_chars: int = 6000) -> str:
    """Failed test names + messages for every failed job of the workflow behind a CircleCI check link.
    This is what lets the classifier see an Elasticsearch 503 for what it is (ZIP-4294 PR #47942 sat on
    'rebase' for hours because the failure text was invisible, 2026-09-16)."""
    wid = circleci_workflow_id(link)
    if not wid:
        return ""
    jobs = _circle(f"/workflow/{wid}/job") or {}
    out = []
    for j in jobs.get("items", []):
        if j.get("status") != "failed" or not j.get("job_number"):
            continue
        out.append(f"## job {j['name']} #{j['job_number']} failed")
        tests = _circle(f"/project/{CIRCLE_PROJECT}/{j['job_number']}/tests") or {}
        for t in tests.get("items", []):
            if t.get("result") == "failure":
                out.append(f"- {t.get('classname')}::{t.get('name')}\n  {(t.get('message') or '')[:500]}")
    return "\n".join(out)[-max_chars:]


def circleci_failed_jobs(link: str) -> list[int]:
    wid = circleci_workflow_id(link)
    jobs = _circle(f"/workflow/{wid}/job") if wid else None
    return [j["job_number"] for j in (jobs or {}).get("items", []) if j.get("status") == "failed" and j.get("job_number")]


def rerun_circleci(link: str) -> bool:
    """Rerun only the failed jobs of the workflow (never an empty commit, never a full rerun)."""
    import os, urllib.request
    wid = circleci_workflow_id(link)
    tok = os.environ.get("CIRCLECI_TOKEN") or _keychain("CIRCLECI_TOKEN")
    if not wid or not tok:
        return False
    req = urllib.request.Request(f"{CIRCLE_API}/workflow/{wid}/rerun", data=json.dumps({"from_failed": True}).encode(),
                                 headers={"Circle-Token": tok, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        return False
