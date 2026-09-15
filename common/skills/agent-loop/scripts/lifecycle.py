"""run-once: advance ONE ticket by one or more lifecycle steps, persisting state after each.

    plan -> plan-review -> implement -> gates -> draft-pr -> ready -> bot-loop -> human-gate-1

Each `step()` call looks at the persisted ticket state, does the single next thing, records the
result, and returns what it did. `run_once()` loops `step()` until the ticket is waiting on
something external (CI running, bot review pending, Human Gate 1, paused, blocked) or the
budget/admission says stop. It is safe to call again at any time: every stage is idempotent
against the remote (existing PR, pushed SHA, adjudicated comment ids).

Only the runner acts. Models are consulted for the plan, its review, the implementation
attempts, and the adjudication of review comments; every decision that matters (accept an
attempt, mark ready, reply, escalate) is taken here from recorded evidence.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import time

import botreview
import ci
import contracts
import plan as plan_mod
import publish
import screenshots
import state

MAX_BOT_ROUNDS = 3


class Step:
    def __init__(self, ticket: str, stage: str, action: str, detail: str = "", wait: bool = False):
        self.ticket, self.stage, self.action, self.detail, self.wait = ticket, stage, action, detail, wait

    def __repr__(self):
        return f"{self.ticket} [{self.stage}] {self.action}" + (f": {self.detail}" if self.detail else "") + (" (waiting)" if self.wait else "")


def _save(cfg, t):
    state.save(cfg.ticket_dir(t.key), t); return t


def _pause(cfg, t, reason):
    if t.state != "paused":
        t = state.transition(t, "paused", reason=reason)
    return _save(cfg, t)


def _manifest_path(wt, key) -> pathlib.Path:
    return pathlib.Path(wt) / "planning" / key.lower() / "tasks.toml"


def _ledger_path(cfg, key) -> pathlib.Path:
    return cfg.ticket_dir(key) / "bot-ledger.json"


def _ci_state_path(cfg, key) -> pathlib.Path:
    return cfg.ticket_dir(key) / "ci.json"


def _load_json(p: pathlib.Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def _task_accepted(cfg, key, task) -> bool:
    """Accepted iff an accepted attempt exists for this id AND that attempt's task.md carries the
    same slug. Task ids are re-used across manifests (a re-plan starts at 001 again); the slug is
    what identifies the work."""
    t = state.load(cfg.ticket_dir(key))
    for e in t.attempts.get(f"{key}/{task.id}", []):
        if e.get("outcome") != "accepted":
            continue
        n = e.get("n")
        task_toml = cfg.state_root / "attempts" / key / task.id / str(n) / "task.toml" if n is not None else None
        if task_toml is None or not task_toml.exists():
            continue
        if f"slug = {json.dumps(task.slug)}" in task_toml.read_text():
            return True
    return False


def _next_task(cfg, key, tasks):
    """First task in manifest order without an accepted attempt (by id AND slug)."""
    for task in tasks:
        if not _task_accepted(cfg, key, task):
            return task
    return None


def step(cfg, runner, ctx, key: str, wt, *, hopper_index: int, pr_number: int | None = None) -> Step:
    tdir = cfg.ticket_dir(key); t = state.load(tdir); t.worktree = str(wt)
    wt = pathlib.Path(wt)

    if state.paused(cfg.state_root):
        return Step(key, t.state, "operator PAUSE", wait=True)
    if (tdir / "HUMAN").exists():
        return Step(key, t.state, "HUMAN takeover file present", wait=True)
    if t.state in ("paused", "blocked"):
        return Step(key, t.state, t.reason, wait=True)

    if t.state in ("queued", "spinup"):
        # worktree already exists in the pilot (spinup is the cmux chain); advance
        while t.state != "plan":
            t = state.transition(t, {"queued": "spinup", "spinup": "plan"}[t.state])
        return Step(key, "spinup", "worktree present", detail=str(wt)) if _save(cfg, t) else None

    if t.state in ("plan", "plan-review"):
        manifest = _manifest_path(wt, key)
        review = manifest.with_name("plan-review.md")
        if manifest.exists() and plan_mod.review_verdict(review) == "approve":
            t = state.transition(t, "plan-review") if t.state == "plan" else t
            t = state.transition(t, "implement"); _save(cfg, t)
            return Step(key, "plan-review", "approved manifest present", detail=str(manifest))
        stage_root = cfg.state_root / "plans" / key / time.strftime("%Y%m%dT%H%M%S")
        log = plan_mod.plan_ticket(cfg, key, wt, hopper_index, stage_root)
        (stage_root / "plan-log.json").write_text(json.dumps(log, indent=2))
        t = state.load(tdir); t.author_vendor = log.get("author_model", ""); t.critic_vendor = log.get("critic_model", "")
        if log.get("result") == "approved":
            if t.state == "plan": t = state.transition(t, "plan-review")
            t = state.transition(t, "implement"); _save(cfg, t)
            # planning/<key>/ is a worktree-local artifact (MEMORY.md: never committed, excluded from
            # PR diff/history). Make sure git ignores it for every worktree of this repo.
            _exclude_planning(wt)
            return Step(key, "plan", "approved", detail=f"{len(log['rounds'])} round(s)")
        if log.get("result") == "blocked":
            t = state.transition(t, "blocked", reason=f"plan: {log.get('reason')}"); _save(cfg, t)
            return Step(key, "plan", "blocked (owner question)", detail=log.get("reason", ""), wait=True)
        return _pause_step(cfg, t, "plan", f"plan failed: {log.get('reason')}")

    if t.state == "implement":
        manifest = _manifest_path(wt, key)
        if not manifest.exists():
            t = state.transition(t, "paused", reason="implement: no manifest"); _save(cfg, t)
            return Step(key, "implement", "no manifest", wait=True)
        tasks = contracts.load_tasks(manifest)
        task = _next_task(cfg, key, tasks)
        if task is None:
            t = state.transition(t, "gates"); _save(cfg, t)
            return Step(key, "implement", "all tasks accepted")
        import runner as runner_mod
        if int(getattr(cfg, "workers_parallel", 1)) > 1:
            import parallel
            def publish_one(tk, paths):
                runner_mod.publish_accepted(cfg, key, str(wt), tk, ensure_pr=False)
            out = parallel.implement_parallel(cfg, runner, ctx, t, tasks, pathlib.Path(wt),
                                              is_done=lambda tk: _task_accepted(cfg, key, tk), publish_one=publish_one,
                                              log=lambda m: print(m, flush=True))
            if out == "accepted":
                t = state.load(tdir); t = state.transition(t, "gates"); _save(cfg, t)
                return Step(key, "implement", "all tasks accepted (parallel)")
            t = state.load(tdir)
            return Step(key, "implement", f"parallel implement -> {out}", detail=t.reason, wait=True)
        seen = sum(1 for x in tasks if x.visual == task.visual and tasks.index(x) < tasks.index(task))
        out = runner.implement_task(ctx, t, task, seen, str(wt))
        if out == "accepted":
            runner_mod.publish_accepted(cfg, key, str(wt), task, ensure_pr=False)   # the draft-pr stage owns PR creation
            return Step(key, "implement", f"task {task.id} accepted + published")
        t = state.load(tdir)
        return Step(key, "implement", f"task {task.id} -> {out}", detail=t.reason, wait=True)

    if t.state == "gates":
        # Deterministic gates beyond per-task verification: worktree prepared, whole ZUI component
        # test dir, rubocop on the branch's Ruby diff, and -- when any task is visual -- Lookbook
        # captures of every scenario of the component (policy: screenshots before leaving draft).
        _sh(["bin/wt", "prepare", "--for", "rails"], wt, 900)
        # `bin/rails test <path>` skips test:prepare, so the webpack CSS/JS build under app/assets/builds
        # can be stale; system tests and the Lookbook captures would then render old styles
        # (found by the plan critic, 2026-09-14). Build explicitly before anything browser-backed.
        rb = _sh(["bash", "-c", "NODE_ENV=development yarn build"], wt, 900)
        if rb.returncode:
            (tdir / "gates.out").write_text((rb.stdout + rb.stderr)[-6000:])
            return _pause_step(cfg, t, "gates", f"gates failed: yarn build rc={rb.returncode}")
        r1 = _sh(["bin/rails", "test", "test/views/components/zui/"], wt, 1800)
        changed = _sh(["git", "diff", "--name-only", "origin/main...HEAD", "--", "*.rb"], wt, 60).stdout.split()
        # --force-exclusion: honour AllCops Exclude (e.g. linters/**) exactly as CI and lefthook do;
        # naming files explicitly would lint excluded paths (gate false-failed on 13 pre-existing offenses).
        r2 = _sh(["bin/agent_run", "rubocop", "--cache", "false", "--force-exclusion", *changed], wt, 600) if changed else None
        ok = r1.returncode == 0 and (r2 is None or r2.returncode == 0)
        (tdir / "gates.out").write_text((r1.stdout + r1.stderr)[-6000:] + ("\n\n" + (r2.stdout + r2.stderr)[-4000:] if r2 else ""))
        if not ok:
            return _pause_step(cfg, t, "gates", f"gates failed: rails test rc={r1.returncode}" + (f", rubocop rc={r2.returncode}" if r2 else ""))
        tasks = contracts.load_tasks(_manifest_path(wt, key)) if _manifest_path(wt, key).exists() else []
        component = _component_name(wt)
        if any(x.visual for x in tasks) and component:
            try:
                base = screenshots.ensure_dev_server(wt)
                shots = screenshots.capture(base, component, tdir / "screenshots" / _head(wt)[:10])
                (tdir / "screenshots.json").write_text(json.dumps({k: str(v) for k, v in shots.items()}))
            except screenshots.VisualGateError as e:
                return _pause_step(cfg, t, "gates", f"visual gate failed: {e}")
        t.evidence_sha = _head(wt); t = state.transition(t, "draft-pr"); _save(cfg, t)
        return Step(key, "gates", "passed", detail=f"evidence_sha={t.evidence_sha[:10]}")

    if t.state == "draft-pr":
        branch = publish.guard_branch(wt, key); publish.push(wt, branch)
        pr = publish.existing_pr(wt, branch)
        title, body = _pr_house_style(cfg, key, wt, t)
        if not pr:
            pr = publish.ensure_draft_pr(wt, branch, title, body)
            publish.record_pr_on_worktree(wt, pr["number"])
        else:
            publish.github_write("pr-edit-body", ["gh", "pr", "edit", str(pr["number"]), "--repo", publish.REPO, "--title", title, "--body", body], wt)
        cis = _load_json(_ci_state_path(cfg, key), {}); cis["pr"] = pr["number"]; _ci_state_path(cfg, key).write_text(json.dumps(cis))
        t = state.transition(t, "ready"); _save(cfg, t)
        return Step(key, "draft-pr", "draft PR present", detail=f"#{pr['number']}")

    if t.state == "ready":
        cis = _load_json(_ci_state_path(cfg, key), {}); pr = cis.get("pr") or pr_number
        checks = ci.fetch_checks(pr, publish.REPO, wt)
        verdict = ci.classify(checks, behind=ci.behind_base(wt), attempts=cis.get("attempts", 0),
                              failure_text=_failure_text(checks, wt), prior_actions=tuple(cis.get("actions", [])))
        cis.setdefault("actions", [])
        if verdict.action == "wait":
            _ci_state_path(cfg, key).write_text(json.dumps(cis)); return Step(key, "ready", "CI running", detail=verdict.reason, wait=True)
        if verdict.action == "green":
            if cis.get("ready_marked_sha") != _head(wt):
                # Tier-3 design decisions (`human_confirm_before_ready`) are NOT a gate on mark-ready:
                # the loop never assigns reviewers, so "ready" only triggers the repo's Claude review
                # bots, and Human Gate 1 -- where Cody confirms these decisions -- still precedes any
                # colleague. They are recorded here and surfaced in the Gate 1 report and the PR body.
                if _manifest_flag(wt, key, "human_confirm_before_ready"):
                    cis["tier3_pending"] = _manifest_header_comment(wt, key)
                try:
                    botreview.mark_ready(pr, wt)
                except publish.PublishError as e:
                    return _pause_step(cfg, t, "ready", f"mark ready failed: {e}")
                cis["ready_marked_sha"] = _head(wt)
            _ci_state_path(cfg, key).write_text(json.dumps(cis))
            t = state.transition(t, "bot-loop"); _save(cfg, t)
            return Step(key, "ready", "CI green; PR marked ready", detail=f"#{pr}")
        cis["attempts"] = cis.get("attempts", 0) + 1; cis["actions"].append(verdict.action); _ci_state_path(cfg, key).write_text(json.dumps(cis))
        if verdict.action == "escalate":
            return _pause_step(cfg, t, "ready", verdict.reason)
        if verdict.action == "rerun":
            for c in checks:
                if c.name in verdict.failing and "actions/runs" in c.link: ci.rerun_gha(c.link, publish.REPO, wt)
            return Step(key, "ready", "CI rerun requested", detail=verdict.reason, wait=True)
        if verdict.action == "rebase":
            r = _sh(["git", "rebase", "origin/main"], wt, 600)
            if r.returncode:
                _sh(["git", "rebase", "--abort"], wt, 60); return _pause_step(cfg, t, "ready", "rebase conflict; needs a human")
            publish.push_rebased(wt, publish.current_branch(wt))
            t = state.transition(t, "gates"); _save(cfg, t)
            return Step(key, "ready", "rebased onto origin/main; re-running gates")
        # fix: write a scoped task and go back to implement
        fix = {"id": f"8{cis['attempts']:02d}", "slug": f"ci-fix-{cis['attempts']}", "summary": f"Fix the CI failure in {', '.join(verdict.failing)} (see feedback)",
               "allowed_files": _sh(["git", "diff", "--name-only", "origin/main...HEAD"], wt, 60).stdout.split()
                                or sorted({g for x in contracts.load_tasks(_manifest_path(wt, key)) for g in x.allowed_files}),
               "may_edit_tests": True, "visual": False,
               "verification_commands": ["bin/rails test test/views/components/zui/"], "acceptance": ["AC-1: the failing CI check's command passes locally"],
               "invariants": ["Do not weaken any test or lint rule"], "stop_when": ["the verification commands pass"]}
        _append_task(_manifest_path(wt, key), fix)
        (cfg.state_root / "attempts" / key / fix["id"]).mkdir(parents=True, exist_ok=True)
        (cfg.state_root / "attempts" / key / fix["id"] / "feedback.md").write_text(f"# CI failure\n\n```\n{_failure_text(checks, wt)[-4000:]}\n```\n")
        t = state.transition(t, "implement", reason=f"ci-fix {fix['id']} queued"); _save(cfg, t)
        return Step(key, "ready", "CI code failure -> fix task queued", detail=fix["id"])

    if t.state == "bot-loop":
        cis = _load_json(_ci_state_path(cfg, key), {}); pr = cis.get("pr") or pr_number
        checks = ci.fetch_checks(pr, publish.REPO, wt)
        if any(c.bucket == "pending" for c in checks):
            return Step(key, "bot-loop", "checks (incl. claude-review) still running", wait=True)
        ledger_p = _ledger_path(cfg, key); ledger = botreview.load_ledger(ledger_p)
        comments = botreview.fetch_bot_comments(pr, wt); todo = botreview.unhandled(comments, ledger)
        if not todo:
            rounds = cis.get("bot_rounds", 0)
            if rounds == 0 and not comments:
                return Step(key, "bot-loop", "no bot comments yet", wait=True)
            t = state.transition(t, "human-gate-1"); _save(cfg, t)
            return Step(key, "bot-loop", "all bot comments adjudicated; CI green", detail="HUMAN GATE 1", wait=True)
        if cis.get("bot_rounds", 0) >= MAX_BOT_ROUNDS:
            return _pause_step(cfg, t, "bot-loop", f"{len(todo)} bot comments outstanding after {MAX_BOT_ROUNDS} rounds")
        # Reuse a paid-for adjudication if the previous round's file already covers every open comment
        # (a later step failed after the critic ran -- do not pay the critic twice for the same comments).
        rnd = cis.get("bot_rounds", 0)
        prev = cfg.state_root / "botreview" / key / f"round-{rnd}" / "adjudication.json" if rnd else None
        decisions = None
        if prev and prev.exists():
            try:
                cand = botreview.parse_adjudication(prev)
                if {c["id"] for c in todo} <= {d.get("id") for d in cand}:
                    decisions = cand; stage = prev.parent
            except RuntimeError:
                decisions = None
        if decisions is None:
            rnd += 1; cis["bot_rounds"] = rnd; _ci_state_path(cfg, key).write_text(json.dumps(cis))
            stage = cfg.state_root / "botreview" / key / f"round-{rnd}"; stage.mkdir(parents=True, exist_ok=True)
            botreview.write_comments_file(todo, stage / "comments.md")
            prompt = botreview.ADJUDICATION_PROMPT.format(key=key, pr=pr, out=stage / "adjudication.json", schema=stage / "schema.toml",
                                                          wt=wt, plan=_manifest_path(wt, key).with_name("plan.md"), comments=stage / "comments.md")
            (stage / "schema.toml").write_text(plan_mod.SCHEMA_EXAMPLE)
            plan_mod._run_agent(cfg, plan_mod.CRITIC_DEF, prompt, stage, wt, 1800, model=t.critic_vendor or None)
            try:
                decisions = botreview.parse_adjudication(stage / "adjudication.json")
            except RuntimeError as e:
                return _pause_step(cfg, t, "bot-loop", f"adjudication unusable: {e}")
        by_id = {c["id"]: c for c in todo}; fixes = []; questions = []
        model_short = (t.critic_vendor or "critic").split("/")[-1]
        for i, d in enumerate(decisions, 1):
            c = by_id.get(d.get("id"))
            if not c: continue
            if d["decision"] == "question":
                questions.append(f"comment {c['id']}: {d.get('reply','')}"); continue
            if d["decision"] == "fix":
                ft = botreview.fix_task_from(d, i)
                if ft: fixes.append(ft)
            try:
                botreview.reply(pr, c, botreview.clean_reply(d.get("reply", "") or f"Addressed in a follow-up commit.", model_short), wt)
            except ValueError as e:
                botreview.save_ledger(ledger_p, ledger)
                return _pause_step(cfg, t, "bot-loop", f"reply rejected: {e}")
            except publish.PublishError as e:
                botreview.save_ledger(ledger_p, ledger)          # keep what was already answered
                return _pause_step(cfg, t, "bot-loop", f"reply failed: {e}")
            ledger[str(c["id"])] = c["body_hash"]
        botreview.save_ledger(ledger_p, ledger)
        if questions:
            return _pause_step(cfg, t, "bot-loop", "reviewer questions need Cody: " + " | ".join(questions)[:600])
        if fixes:
            for ft in fixes: _append_task(_manifest_path(wt, key), ft)
            t = state.transition(t, "implement", reason=f"{len(fixes)} review fix task(s) queued"); _save(cfg, t)
            return Step(key, "bot-loop", f"{len(fixes)} fix task(s) queued from review; back to implement")
        return Step(key, "bot-loop", f"{len(decisions)} comment(s) answered; none required code changes")

    if t.state == "human-gate-1":
        return Step(key, "human-gate-1", "waiting for Cody", wait=True)
    return Step(key, t.state, "no handler", wait=True)


def run_once(cfg, runner, ctx, key: str, wt, *, hopper_index: int, max_steps: int = 12) -> list[Step]:
    steps = []
    for _ in range(max_steps):
        s = step(cfg, runner, ctx, key, wt, hopper_index=hopper_index)
        steps.append(s); print(s, flush=True)
        if s.wait:
            break
    return steps


# ----------------------------------------------------------------------------- helpers

def _pause_step(cfg, t, stage, reason) -> Step:
    _pause(cfg, t, reason); return Step(t.key, stage, "paused", detail=reason, wait=True)


def _sh(cmd, cwd, timeout):
    return subprocess.run(["bash", "-lc", " ".join(__import__("shlex").quote(c) for c in cmd)], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _head(wt) -> str:
    return _sh(["git", "rev-parse", "HEAD"], wt, 30).stdout.strip()


def _failure_text(checks, wt) -> str:
    parts = []
    for c in checks:
        if c.bucket in ("fail", "cancel") and "actions/runs" in c.link:
            parts.append(ci.gha_failure_excerpt(c.link, publish.REPO, wt))
    return "\n".join(parts)


def _manifest_flag(wt, key, flag) -> bool:
    p = _manifest_path(wt, key)
    if not p.exists(): return False
    import tomllib
    return bool(tomllib.loads(p.read_text()).get(flag))


def _append_task(manifest: pathlib.Path, task: dict) -> None:
    botreview.write_fix_manifest([task], manifest.with_name("_append.toml"))
    body = manifest.with_name("_append.toml").read_text().split("\n", 1)[1]
    with open(manifest, "a") as f:
        f.write("\n" + body)
    manifest.with_name("_append.toml").unlink()


def _component_name(wt) -> str | None:
    """`app/views/components/zui/<name>/` added on this branch."""
    out = _sh(["git", "diff", "--name-only", "origin/main...HEAD", "--", "app/views/components/zui/"], wt, 60).stdout.split()
    names = sorted({p.split("/")[4] for p in out if p.count("/") >= 5})
    return names[0] if names else None


def _plan_section(plan_md: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", plan_md, re.M | re.S)
    return m.group(1).strip() if m else ""


def _ticket_summary(cfg, key) -> str:
    for d in sorted((cfg.state_root / "plans" / key).glob("*/ticket.json"), reverse=True):
        try:
            return json.loads(d.read_text())["fields"]["summary"]
        except Exception:  # noqa: BLE001
            continue
    return key


def insert_task_before(manifest: pathlib.Path, task: dict, before_id: str) -> None:
    """Insert a prerequisite fix task immediately before `before_id` in manifest order. The runner
    dispatches tasks in file order, so a contract defect found in a LATER task (007 found 001's
    tag enum wrong, 2026-09-14) is repaired before that later task runs again."""
    botreview.write_fix_manifest([task], manifest.with_name("_insert.toml"))
    body = manifest.with_name("_insert.toml").read_text().split("\n", 1)[1].strip("\n")
    manifest.with_name("_insert.toml").unlink()
    text = manifest.read_text()
    marker = f'\n[[tasks]]\nid = {json.dumps(before_id)}'
    i = text.find(marker)
    if i < 0:
        raise ValueError(f"task {before_id} not found in {manifest}")
    manifest.write_text(text[:i] + "\n" + body + "\n" + text[i:])


def _manifest_header_comment(wt, key) -> str:
    """The planner's owner/design questions live as a comment block at the top of tasks.toml."""
    p = _manifest_path(wt, key)
    if not p.exists():
        return ""
    lines = []
    for ln in p.read_text().splitlines():
        if ln.startswith("#"):
            lines.append(ln.lstrip("# ").rstrip())
        elif ln.strip() and not ln.startswith("human_confirm_before_ready"):
            break
    return " ".join(lines).strip()


def _exclude_planning(wt) -> None:
    """Add `planning/` to the repo's shared info/exclude (common git dir, so linked worktrees share it)."""
    common = _sh(["git", "rev-parse", "--git-common-dir"], wt, 30).stdout.strip()
    if not common:
        return
    p = pathlib.Path(common) if pathlib.Path(common).is_absolute() else pathlib.Path(wt) / common
    ex = p / "info" / "exclude"; ex.parent.mkdir(parents=True, exist_ok=True)
    cur = ex.read_text() if ex.exists() else ""
    if "planning/" not in cur.splitlines():
        ex.write_text(cur.rstrip("\n") + "\nplanning/\n")


HOUSE_STYLE_EXAMPLE_PR = 47860     # a PR of Cody's whose structure/voice is the target


def _pr_house_style(cfg, key, wt, t) -> tuple[str, str]:
    import prbody
    summary = _ticket_summary(cfg, key)
    component = _component_name(wt) or "component"
    title = publish.pr_title(key, f"Add ZUI {component.replace('_', ' ').title()} component")
    shots_p = cfg.ticket_dir(key) / "screenshots.json"; shots_md = None
    if shots_p.exists():
        shots = {k: pathlib.Path(v) for k, v in json.loads(shots_p.read_text()).items()}
        shots_md = screenshots.screenshots_table(shots, screenshots.upload(list(shots.values()), wt))
    gates_out = (cfg.ticket_dir(key) / "gates.out").read_text(errors="replace") if (cfg.ticket_dir(key) / "gates.out").exists() else ""
    example = _sh(["gh", "pr", "view", str(HOUSE_STYLE_EXAMPLE_PR), "--repo", publish.REPO, "--json", "body", "--jq", ".body"], wt, 60).stdout
    stage = cfg.state_root / "prbody" / key / time.strftime("%Y%m%dT%H%M%S")
    try:
        body = prbody.write_body(cfg, key, wt, t, summary, example, shots_md, gates_out, stage)
    except prbody.BodyRejected as e:
        print(f"{key}: PR body rejected twice ({e}); using the deterministic fallback", file=sys.stderr)
        files = _sh(["git", "diff", "--name-only", "origin/main...HEAD"], wt, 60).stdout.split()
        body = prbody.fallback_body(key, summary, [f for f in files if f.startswith("app/")], shots_md)
    return title, body

