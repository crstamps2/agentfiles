"""Worker result.md parsing (lenient format, strict meaning) and tasks.toml validation."""
from __future__ import annotations
import dataclasses
import re
import tomllib

STATUSES = {"pass", "fail", "blocked"}
REASONS = {"none", "implementation", "test", "environment", "timeout", "owner", "protocol"}
_KEY_RE = re.compile(r"^\s*(?:[#*\-]+\s*)?(status|reason|base|files|evidence|unverified|next)\s*[:=]\s*(.*)$", re.I)
MAX_EXTRA_VERIFICATION = 3
_ID_RE = re.compile(r"^\d{3}[a-z]?$")   # optional letter suffix: a prerequisite fix inserted between planned tasks (006a)


class ProtocolError(ValueError):
    pass


class ManifestError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Result:
    status: str
    reason: str = ""
    base: str = ""
    files: list = dataclasses.field(default_factory=list)
    evidence: str = ""
    unverified: str = ""
    next: str = ""


def parse_result(text: str) -> Result:
    found = {}
    for line in text.splitlines():
        m = _KEY_RE.match(line)
        if m:
            # first occurrence wins; later duplicates are ignored
            found.setdefault(m.group(1).lower(), m.group(2).strip())
    status = found.get("status", "").lower()
    if status not in STATUSES:
        raise ProtocolError(f"result.md missing or invalid STATUS (got {found.get('status')!r})")
    reason = found.get("reason", "").lower().strip()
    # Known reasons are normalised; an UNKNOWN reason is kept verbatim (bounded) rather than erased --
    # a worker's `blocked / REASON: stale-todo` is a contract complaint the runner routes to plan repair,
    # and the label is the evidence (ZIP-7872/005).
    if reason not in REASONS:
        reason = re.sub(r"[^a-z0-9_-]+", "-", reason)[:40].strip("-") or ""
    files = [f.strip() for f in re.split(r"[,\s]+", found.get("files", "")) if f.strip()]
    return Result(status=status, reason=reason, base=found.get("base", ""), files=files,
                  evidence=found.get("evidence", ""), unverified=found.get("unverified", ""),
                  next=found.get("next", ""))


@dataclasses.dataclass(frozen=True)
class Task:
    id: str
    slug: str
    summary: str
    allowed_files: list
    verification_commands: list
    acceptance: list
    may_edit_tests: bool = False
    visual: bool = False
    size: str = "normal"          # "small" => free local model first (1-2 existing files, no browser)
    after: list = dataclasses.field(default_factory=list)   # task ids that must LAND before this one runs (implied when allowed_files overlap)
    timeout_s: int = 4500
    invariants: list = dataclasses.field(default_factory=list)
    out_of_scope: list = dataclasses.field(default_factory=list)
    stop_when: list = dataclasses.field(default_factory=list)
    figma_nodes: list = dataclasses.field(default_factory=list)


_REQ_STR = ("id", "slug", "summary")
_REQ_LIST = ("allowed_files", "verification_commands", "acceptance")


def validate_tasks(data: dict) -> list:
    errs = []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list) or not tasks:
        return ["tasks: manifest must contain at least one [[tasks]] entry"]
    seen = set()
    prev = ""
    allowed_keys = {f.name for f in dataclasses.fields(Task)}
    for i, t in enumerate(tasks):
        p = f"tasks[{i}]"
        if not isinstance(t, dict):
            errs.append(f"{p}: must be a table/dict")
            continue
        for k in _REQ_STR:
            if not isinstance(t.get(k), str) or not t.get(k).strip():
                errs.append(f"{p}.{k}: required non-empty string")
        for k in _REQ_LIST:
            v = t.get(k)
            if not isinstance(v, list) or not v:
                errs.append(f"{p}.{k}: required non-empty list")
        for k in ("allowed_files", "verification_commands", "acceptance", "invariants", "out_of_scope", "stop_when", "figma_nodes"):
            if k in t and (not isinstance(t[k], list) or any(not isinstance(x, str) or not x.strip() for x in t[k])):
                errs.append(f"{p}.{k}: every entry must be a non-empty string")
        for path in t.get("allowed_files", []) if isinstance(t.get("allowed_files"), list) else []:
            if not isinstance(path, str):
                continue
            parts = path.replace("\\", "/").split("/")
            if path.startswith("/") or ".." in parts:
                errs.append(f"{p}.allowed_files: must be relative and contain no .. segment ({path!r})")
            # A glob as the FIRST segment is repository-wide authority, not an allowlist: `["**"]`
            # passed validation and let a task edit every unprotected file (branch review 2026-09-23).
            if parts and ("*" in parts[0] or parts[0] in ("", ".")):
                errs.append(f"{p}.allowed_files: first path segment must be a concrete directory, "
                            f"not a glob ({path!r})")
        for k in t:
            if k not in allowed_keys:
                errs.append(f"{p}: unknown key {k!r}")
        # Verification must use the repository's own tools, not planner-written probes: a manifest
        # whose commands reference planning/ is a test harness, not a plan (ZIP-7872, 2026-09-15).
        cmds = t.get("verification_commands") or []
        if isinstance(cmds, list):
            for c in cmds:
                for e in validate_command(c) if isinstance(c, str) else []:
                    errs.append(f"{p}.verification_commands: {e}")
            for c in cmds:
                if isinstance(c, str) and re.search(r"(^|[\s'\"=/])planning/", c):
                    errs.append(f"{p}.verification_commands: references planning/ ({c[:60]!r}); use repo tools or a task-owned test")
            for c in cmds:
                if isinstance(c, str) and re.search(r"(^|[\s;&|]|bin/agent_run\s+|bundle exec\s+)(rubocop|reek)\s", c) and "--force-exclusion" not in c:   # INVOCATIONS only, not paths like linters/rubocop/ or .rubocop.yml
                    errs.append(f"{p}.verification_commands: rubocop/reek must pass --force-exclusion (explicit paths bypass AllCops Exclude, e.g. linters/**): {c[:70]!r}")
            extra = [c for c in cmds if isinstance(c, str) and not re.search(r"bin/wt prepare|rails test|rubocop|git diff --check|reek|yarn (build|prettier|eslint)|slim-lint|stylelint", c)]
            if len(extra) > MAX_EXTRA_VERIFICATION:
                errs.append(f"{p}.verification_commands: {len(extra)} bespoke commands (max {MAX_EXTRA_VERIFICATION} beyond prepare/test/lint); fold checks into a task-owned test")
        if t.get("size", "normal") not in ("normal", "small"):
            errs.append(f"{p}.size: must be 'normal' or 'small' (got {t.get('size')!r})")
        if t.get("size") == "small":
            af = t.get("allowed_files") or []
            if not isinstance(af, list) or len(af) > 2 or any("*" in str(x) for x in af) or t.get("visual"):
                errs.append(f"{p}.size: 'small' requires <=2 explicit allowed_files (no globs) and visual = false")
        aft = t.get("after", [])
        if not isinstance(aft, list) or any(not isinstance(a, str) for a in aft):
            errs.append(f"{p}.after: must be a list of task ids")
        tid = str(t.get("id", ""))
        if tid and not _ID_RE.match(tid):
            errs.append(f"{p}.id: must match ^\\d{{3}}[a-z]?$ (got {tid!r})")
        if tid in seen:
            errs.append(f"{p}.id: duplicate id {tid}")
        seen.add(tid)
        if tid and prev and tid <= prev:
            errs.append(f"{p}.id: ids must be ascending ({prev} then {tid})")
        prev = tid or prev
        for k in ("may_edit_tests", "visual"):
            if k in t and not isinstance(t[k], bool):
                errs.append(f"{p}.{k}: must be boolean")
        if "timeout_s" in t and (isinstance(t["timeout_s"], bool) or not isinstance(t["timeout_s"], int) or t["timeout_s"] <= 0):
            errs.append(f"{p}.timeout_s: must be positive int")
    return errs


def load_tasks(path) -> list:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    errs = validate_tasks(data)
    if errs:
        raise ManifestError(f"{path}:\n  " + "\n  ".join(errs))
    fields = {f.name for f in dataclasses.fields(Task)}
    return [Task(**{k: v for k, v in t.items() if k in fields}) for t in data["tasks"]]

# --- verification command grammar -------------------------------------------------------------
# A verification command is EXECUTED by the runner. Before this boundary existed, any string was
# accepted and handed to `/bin/sh -c`, so a planner or review adjudicator (whose inputs include
# Jira text and PR review-bot comments) could run anything as the unattended account -- `gh api`
# merges, force-pushes, credential reads. Reproduced during the branch review 2026-09-23 with
# `allowed_files = ["**"]` and `"true; gh api --method POST .../merges"`.
#
# What is allowed, deliberately matching how real manifests are written: optional KEY=VALUE env
# prefixes, simple commands joined by `&&`, glob characters in arguments, and an allowlist of the
# repository's own tools. What is refused: every other shell construct (`;`, `|`, `&`, command
# substitution, redirection, newlines) and every executable that is a general-purpose interpreter.
SHELL_ESCAPES = (
    (r";", "command separator ';'"),
    (r"\|", "pipe '|'"),
    (r"(?<!&)&(?!&)", "background/duplicate '&'"),
    (r"\$\(", "command substitution '$('"),
    (r"`", "command substitution backtick"),
    (r"\$\{", "parameter expansion '${'"),
    (r"[<>]", "redirection"),
    (r"[\n\r]", "newline"),
)
# `bin/**` is the repository's own tool directory: version-controlled, reviewed, and the normal way
# a gate is written. Everything else must be named explicitly.
# Bounded utilities: no interpreter, no network client, no `find` (-exec re-opens execution), no
# rm/cp/mv. Writes from touch/mkdir remain subject to the task's allowed_files diff check.
ALLOWED_EXECUTABLES = {"bundle", "yarn", "npx", "git", "grep", "test", "true", "echo",
                       "sleep", "touch", "mkdir", "printf", "ls", "cat", "head", "tail", "wc", "diff"}


def _executable_allowed(exe: str) -> bool:
    return (exe in ALLOWED_EXECUTABLES or exe in WRAPPED_TOOLS
            or (exe.startswith("bin/") and ".." not in exe.split("/")))
# Read-only git only: a verification gate never needs to move a ref.
GIT_VERBS = {"diff", "status", "log", "rev-parse", "ls-files", "show", "grep", "describe", "cat-file"}
WRAPPED_TOOLS = {"rubocop", "reek", "rails", "rspec", "ruby", "i18n-tasks", "slim-lint", "stylelint",
                 "prettier", "eslint", "jest", "erblint", "brakeman", "yarn", "npx"}
ENV_PREFIX = re.compile(r"^[A-Z_][A-Z0-9_]*=[\w./:@-]*$")
# `rails runner` executes Ruby in the app. It is how plans pin public contracts, so it stays --
# but Ruby that shells out or writes files is not a verification gate.
# `system` etc. followed by `/` is a path segment such as "system/front_end", not a call.
RUBY_ESCAPE = re.compile(r"\b(system|exec|spawn)\b(?!/)|%x|Open3|Kernel\.|IO\.popen|\bopen\(\s*['\"]\||FileUtils|"
                         r"File\.(write|open|delete)|require\s+['\"](net/|open-uri|socket)|`", re.I)


def validate_command(cmd: str) -> list[str]:
    """Errors for one verification command; empty list means it may be executed."""
    import shlex
    errs = []
    if not isinstance(cmd, str) or not cmd.strip():
        return ["empty command"]
    for pat, what in SHELL_ESCAPES:
        if re.search(pat, cmd):
            errs.append(f"{what} is not allowed in a verification command ({cmd[:60]!r})")
    if errs:
        return errs
    for segment in cmd.split("&&"):
        seg = segment.strip()
        if not seg:
            errs.append(f"empty segment in {cmd[:60]!r}")
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError as e:
            errs.append(f"unparsable command ({e}) in {seg[:60]!r}")
            continue
        while tokens and ENV_PREFIX.match(tokens[0]):
            tokens.pop(0)
        if not tokens:
            errs.append(f"only environment assignments in {seg[:60]!r}")
            continue
        # `bin/agent_run` is a transparent wrapper (`bin/agent_run bundle exec reek ...`): unwrap it
        # and judge what it actually runs, rather than special-casing each nesting.
        while tokens and tokens[0] == "bin/agent_run":
            tokens = tokens[1:]
            if not tokens:
                errs.append(f"bin/agent_run wraps nothing in {seg[:60]!r}")
        if not tokens:
            continue
        exe, args = tokens[0], tokens[1:]
        if exe.startswith("/") or ".." in exe.split("/"):
            errs.append(f"absolute or traversing executable path {exe!r}")
            continue
        if not _executable_allowed(exe):
            errs.append(f"executable {exe!r} is not a repository tool (allowed: bin/**, "
                        f"{', '.join(sorted(ALLOWED_EXECUTABLES))})")
            continue
        # An inline-code flag only matters where it feeds an INTERPRETER (`bundle exec ruby -e`,
        # `npx ts-node -e`). `test ! -e path` and `grep -e pattern` are ordinary predicates.
        INTERPRETERS = ("ruby", "node", "python", "python3", "ts-node", "sh", "bash")
        interpreter = exe in INTERPRETERS or (
            exe in ("bundle", "yarn", "npx") and any(a in INTERPRETERS for a in args[:2]))
        if interpreter and any(a in ("-c", "-e", "--eval", "--command") for a in args):
            errs.append(f"inline interpreter code in {seg[:60]!r}: a gate runs repository tools, not a script")
        if exe == "git":
            verb = next((a for a in args if not a.startswith("-")), "")
            if verb not in GIT_VERBS:
                errs.append(f"git {verb!r} is not read-only (allowed: {', '.join(sorted(GIT_VERBS))})")
        if exe == "bundle":
            if not args or args[0] != "exec" or len(args) < 2 or args[1] not in WRAPPED_TOOLS:
                errs.append(f"bundle must be `bundle exec <repo tool>` in {seg[:60]!r}")
        if exe == "bin/rails" and args[:1] == ["runner"]:
            code = " ".join(args[1:])
            if RUBY_ESCAPE.search(code):
                errs.append(f"rails runner code may not shell out or write files ({code[:60]!r})")
    return errs

