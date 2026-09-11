"""Load hopper.toml into a typed Config and own the on-disk state layout."""
from __future__ import annotations
import dataclasses
import os
import pathlib
import re
import tomllib

DEFAULT_PATH = pathlib.Path(__file__).resolve().parent.parent / "hopper.toml"
REQUIRED_TABLES = ("hopper", "paths", "timeouts", "admission", "budgets", "protection", "arms", "outcomes")


class ConfigError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class TicketSpec:
    key: str
    deps: list = dataclasses.field(default_factory=list)
    pin_arm: str | None = None


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
    cloud_starter_credit_usd: float
    ci_rerun_max: int


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
    arms_alternate: list
    outcomes: dict
    local_model: str | None = None
    local_unload_after_attempt: bool = True

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
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    missing = [t for t in REQUIRED_TABLES if t not in raw]
    if missing:
        raise ConfigError(f"{path}: missing tables {missing}")
    h, p, t, a, b, pr, arms, oc = (raw[k] for k in REQUIRED_TABLES)
    tickets = {}
    for row in h.get("tickets", []):
        spec = TicketSpec(key=row["key"], deps=list(row.get("deps", [])), pin_arm=row.get("pin_arm"))
        tickets[spec.key] = spec
    local_model = None
    local_unload_after_attempt = True
    if "local" in raw:
        local_table = raw["local"]
        local_model = local_table.get("model")
        if not local_model:
            raise ConfigError("[local].model is required and must be ctx-pinned (…-ctx32k:…) when [local] is present")
        if not re.search(r"-ctx32k:", local_model):
            raise ConfigError(f"local model must be a ctx-pinned derived model (…-ctx32k:…); Ollama's default 4K context truncates pi's system prompt")
        local_unload_after_attempt = bool(local_table.get("unload_after_attempt", True))
    return Config(
        epic=h["epic"],
        owned_epics=list(h.get("owned_epics", [h["epic"]])),
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
        arms_alternate=list(arms["alternate"]),
        outcomes=dict(oc),
        local_model=local_model,
        local_unload_after_attempt=local_unload_after_attempt,
    )
