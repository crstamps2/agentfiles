"""Load hopper.toml into a typed Config and own the on-disk state layout."""
from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import tomllib

DEFAULT_PATH = pathlib.Path(__file__).resolve().parent.parent / "hopper.toml"
EXAMPLE_PATH = DEFAULT_PATH.with_name("hopper.example.toml")
REQUIRED_TABLES = ("hopper", "paths", "timeouts", "admission", "budgets", "protection", "arms", "outcomes")


class ConfigError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class TicketSpec:
    key: str
    deps: list = dataclasses.field(default_factory=list)
    pin_arm: str | None = None
    exclude: str | None = None      # reason string => never drip this ticket


@dataclasses.dataclass(frozen=True)
class AdmissionThresholds:
    compressor_pct_max: float
    load_per_core_max: float
    disk_free_gb_min: float
    require_ac_power: bool
    defer_max_s: int


@dataclasses.dataclass(frozen=True)
class Budgets:
    premium_daily_usd: float
    premium_monthly_usd: float
    total_unattended_daily_usd: float
    ci_rerun_max: int
    cloud_starter_credit_usd: float = 0.0
    preimplementation_usd: float = 30.0   # advisory spend warning before the first accepted task


@dataclasses.dataclass(frozen=True)
class Config:
    epic: str
    owned_epics: list
    tickets: dict            # key -> TicketSpec, insertion-ordered
    state_root: pathlib.Path
    cmux_chain_dir: pathlib.Path
    pi_agents_dir: pathlib.Path
    heavy_stage_timeout_s: int
    light_stage_timeout_s: int
    admission: AdmissionThresholds
    budgets: Budgets
    protected_paths: list
    test_path_globs: list
    harness_artifact_globs: list
    arms_alternate: list
    outcomes: dict
    local_model: str | None = None
    local_unload_after_attempt: bool = True
    workers_parallel: int = 1        # concurrent worker (light) stages; verification stays serialised on the heavy lane
    drip: dict = dataclasses.field(default_factory=dict)
    cohort_capacity: int = 4
    hard_capacity: int = 7
    workspace_backend: str = "orca"
    workspace_repo: pathlib.Path = dataclasses.field(default_factory=lambda: pathlib.Path("~/workspace/example-app").expanduser())
    workspace_statuses: dict = dataclasses.field(default_factory=dict)
    workspace_limit: int | None = None
    project_configured: bool = False
    github_repo: str = "example-org/example-app"
    jira_base_url: str = "https://example.atlassian.net/browse"
    operator_email: str = "operator@example.org"
    worktrees_root: pathlib.Path = dataclasses.field(default_factory=lambda: pathlib.Path("~/workspace/worktrees").expanduser())
    circleci_project: str = "gh/example-org/example-app"
    pr_footer: str = "— opened by the agent loop on the operator's behalf"
    house_style_example_pr: int | None = None

    def ticket_index(self, key: str) -> int:
        """1-based hopper position, else the ticket number: drives author/critic vendor alternation, so
        tickets the drip pulls from outside the hopper list must not all get the same index."""
        keys = list(self.tickets)
        if key in keys:
            return keys.index(key) + 1
        m = re.search(r"-(\d+)$", key)
        return int(m.group(1)) if m else 1

    def ticket_dir(self, key: str) -> pathlib.Path:
        return self.state_root / "tickets" / key

    def ensure_dirs(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_root, 0o700)
        for sub in ("tickets", "locks", "attempts"):
            (self.state_root / sub).mkdir(exist_ok=True)


def _p(s: str) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(s)).resolve()


def load(path: pathlib.Path | None = None) -> Config:
    path = pathlib.Path(path) if path else DEFAULT_PATH
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError as exc:
        raise ConfigError(f"Missing local configuration: {path}. Copy hopper.example.toml to hopper.toml and configure it before running.") from exc
    missing = [t for t in REQUIRED_TABLES if t not in raw]
    if missing:
        raise ConfigError(f"{path}: missing tables {missing}")
    h, p, t, a, b, pr, arms, oc = (raw[k] for k in REQUIRED_TABLES)
    tickets = {}
    for row in h.get("tickets", []):
        spec = TicketSpec(key=row["key"], deps=list(row.get("deps", [])), pin_arm=row.get("pin_arm"), exclude=row.get("exclude"))
        tickets[spec.key] = spec
    local_model = None
    local_unload_after_attempt = True
    if "local" in raw:
        local_table = raw["local"]
        local_model = local_table.get("model")
        if not local_model:
            raise ConfigError("[local].model is required and must be ctx-pinned (…-ctx32k:…) when [local] is present")
        if not re.search(r"-ctx32k:", local_model):
            raise ConfigError("local model must be a ctx-pinned derived model (…-ctx32k:…); Ollama's default 4K context truncates pi's system prompt")
        local_unload_after_attempt = bool(local_table.get("unload_after_attempt", True))
    drip = dict(raw.get("drip", {}))
    if int(drip.get("max_in_flight", 3)) > 7:
        raise ConfigError(f"{path}: [drip].max_in_flight {drip['max_in_flight']} exceeds the hard cap of 7 tickets")
    capacity = int(raw.get("cohort", {}).get("capacity", 4))
    if not 1 <= capacity <= 7:
        raise ConfigError(f"{path}: [cohort].capacity {capacity} must be between 1 and the hard cap of 7")
    owned = list(h.get("owned_epics", [h["epic"]]))
    unowned = [e for e in drip.get("epics", []) if e not in owned]
    if unowned:
        raise ConfigError(f"{path}: [drip].epics {unowned} are not in [hopper].owned_epics; the spinup "
                          "work-listener would spin their tickets up as well")
    workspace = raw.get("workspace", {})
    if not isinstance(workspace, dict) or workspace.get("backend", "orca") not in ("orca", "cmux"):
        raise ConfigError("[workspace].backend must be orca or cmux")
    statuses = workspace.get("statuses", {})
    if not isinstance(statuses, dict) or any(not isinstance(v, str) or not v.strip() for v in statuses.values()):
        raise ConfigError("[workspace.statuses] IDs must be nonempty strings")
    workspace_limit = workspace.get("max_open")
    if workspace_limit is not None and (type(workspace_limit) is not int or not 1 <= workspace_limit <= 10):
        raise ConfigError("[workspace].max_open must be an integer between 1 and 10")
    project = raw.get("project", {})
    if not isinstance(project, dict):
        raise ConfigError("[project] must be a table")
    configured = project.get("configured", False)
    if type(configured) is not bool:
        raise ConfigError("[project].configured must be a boolean")
    if configured:
        required = ("github_repo", "jira_base_url", "operator_email", "worktrees_root")
        if any(not isinstance(project.get(name), str) or not project[name].strip() for name in required):
            raise ConfigError(f"Configured [project] requires nonempty string values for {required}")
    github_repo = project.get("github_repo", "example-org/example-app")
    if not isinstance(github_repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github_repo):
        raise ConfigError("[project].github_repo must be owner/repository")
    from urllib.parse import urlsplit
    jira_base = project.get("jira_base_url", "https://example.atlassian.net/browse")
    if not isinstance(jira_base, str):
        raise ConfigError("[project].jira_base_url must be an HTTP(S) issue-link base")
    parsed = urlsplit(jira_base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("[project].jira_base_url must be an HTTP(S) issue-link base without credentials, query, or fragment")
    example_pr = project.get("house_style_example_pr")
    if example_pr is not None and (type(example_pr) is not int or example_pr <= 0):
        raise ConfigError("[project].house_style_example_pr must be a positive integer")
    repo = workspace.get("repo", "~/workspace/example-app")
    if not isinstance(repo, str) or not repo.strip():
        raise ConfigError("[workspace].repo must be a nonempty path")
    return Config(
        epic=h["epic"],
        owned_epics=owned,
        tickets=tickets,
        state_root=_p(p["state_root"]),
        cmux_chain_dir=_p(p["cmux_chain_dir"]),
        pi_agents_dir=_p(p["pi_agents_dir"]),
        heavy_stage_timeout_s=int(t["heavy_stage_s"]),
        light_stage_timeout_s=int(t["light_stage_s"]),
        admission=AdmissionThresholds(**a),
        budgets=Budgets(**b),
        protected_paths=list(pr["protected_paths"]),
        test_path_globs=list(pr["test_path_globs"]),
        harness_artifact_globs=list(pr.get("harness_artifact_globs", [])),
        arms_alternate=list(arms["alternate"]), workers_parallel=int(arms.get("workers_parallel", 1)), drip=drip,
        cohort_capacity=capacity,
        outcomes=dict(oc),
        local_model=local_model,
        local_unload_after_attempt=local_unload_after_attempt,
        workspace_backend=workspace.get("backend", "orca"),
        workspace_repo=_p(repo),
        workspace_statuses=dict(statuses),
        workspace_limit=workspace_limit,
        project_configured=configured,
        github_repo=github_repo,
        jira_base_url=jira_base.rstrip("/"),
        operator_email=str(project.get("operator_email", "operator@example.org")),
        worktrees_root=_p(project.get("worktrees_root", "~/workspace/worktrees")),
        circleci_project=str(project.get("circleci_project", f"gh/{github_repo}")),
        pr_footer=str(project.get("pr_footer", "— opened by the agent loop on the operator's behalf")),
        house_style_example_pr=example_pr,
    )


def configure_project(cfg: Config) -> None:
    """Bind legacy runtime consumers once, after loading the explicit local config."""
    if not cfg.project_configured:
        raise ConfigError("Configure [project] in your local hopper.toml and set configured = true before running; hopper.example.toml is setup-only.")
    import ci
    import drip
    import publish
    publish.REPO = cfg.github_repo
    publish.JIRA_BASE = cfg.jira_base_url
    publish.FOOTER = cfg.pr_footer
    ci.CIRCLE_PROJECT = cfg.circleci_project
    drip.ME = cfg.operator_email
    drip.WORKTREES = cfg.worktrees_root
    drip.APP_REPO = cfg.workspace_repo
