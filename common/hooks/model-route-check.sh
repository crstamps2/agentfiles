#!/bin/bash
set -u

strict=0
if [ "${1-}" = "--strict" ]; then
  strict=1
elif [ "$#" -ne 0 ]; then
  echo "usage: model-route-check.sh [--strict]" >&2
  exit 2
fi

repo=${AF_REPO:-$(CDPATH='' cd -- "$(dirname "$0")/../.." && pwd -P)}

python3 - "$strict" "$repo" <<'PY'
from __future__ import annotations

import fnmatch
import json
import os
import pathlib
import re
import subprocess
import sys
import tomllib

THINKING = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
strict = sys.argv[1] == "1"
repo = pathlib.Path(sys.argv[2])
home = pathlib.Path.home()
agent_dir = home / ".pi" / "agent"


def display(path: pathlib.Path) -> str:
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def normalized(route: str) -> str:
    value = (route or "").strip().strip('"').strip("'")
    head, sep, tail = value.rpartition(":")
    if sep and tail in THINKING and "/" in head:
        return head
    return value


def add(routes: list[tuple[str, str]], source: str, route: object) -> None:
    if isinstance(route, str) and route.strip():
        routes.append((source, normalized(route)))


try:
    registry_timeout_s = float(os.environ.get("PI_MODEL_ROUTE_CHECK_TIMEOUT_S", "20"))
except ValueError:
    registry_timeout_s = 20.0

try:
    registry = subprocess.run(
        ["pi", "--list-models"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=registry_timeout_s,
    )
except subprocess.TimeoutExpired:
    print(
        "MODEL ROUTE CHECK: could not read active Pi model registry: "
        f"timed out after {registry_timeout_s:g}s"
    )
    raise SystemExit(1 if strict else 0)
except OSError as exc:
    detail = exc.strerror or type(exc).__name__
    print(f"MODEL ROUTE CHECK: could not read active Pi model registry: {detail}")
    raise SystemExit(1 if strict else 0)

if registry.returncode:
    detail = (registry.stderr or registry.stdout).strip().splitlines()
    suffix = f" {detail[-1][:240]}" if detail else f" exit {registry.returncode}"
    print(f"MODEL ROUTE CHECK: could not read active Pi model registry:{suffix}")
    raise SystemExit(1 if strict else 0)

available: set[str] = set()
for line in registry.stdout.splitlines():
    fields = line.split()
    if len(fields) >= 2 and fields[0] != "provider":
        available.add(f"{fields[0]}/{fields[1]}")

routes: list[tuple[str, str]] = []
config_errors: list[str] = []

settings_path = agent_dir / "settings.json"
if settings_path.exists():
    try:
        settings = json.loads(settings_path.read_text())
        default_model = settings.get("defaultModel")
        default_provider = settings.get("defaultProvider")
        if isinstance(default_model, str):
            route = default_model if "/" in default_model else (
                f"{default_provider}/{default_model}" if isinstance(default_provider, str) else ""
            )
            add(routes, f"{display(settings_path)}:defaultModel", route)
        subagents = settings.get("subagents") or {}
        if isinstance(subagents, dict):
            add(routes, f"{display(settings_path)}:subagents.defaultModel", subagents.get("defaultModel"))
            overrides = subagents.get("agentOverrides") or {}
            if isinstance(overrides, dict):
                for name, values in overrides.items():
                    if isinstance(values, dict):
                        add(
                            routes,
                            f"{display(settings_path)}:subagents.agentOverrides.{name}.model",
                            values.get("model"),
                        )
    except (OSError, json.JSONDecodeError) as exc:
        config_errors.append(f"{display(settings_path)}: {type(exc).__name__}")

models_path = agent_dir / "models.json"
if models_path.exists():
    try:
        providers = (json.loads(models_path.read_text()).get("providers") or {})
        if isinstance(providers, dict):
            for provider, values in providers.items():
                if not isinstance(values, dict):
                    continue
                overrides = values.get("modelOverrides") or {}
                if isinstance(overrides, dict):
                    for model in overrides:
                        add(
                            routes,
                            f"{display(models_path)}:providers.{provider}.modelOverrides.{model}",
                            f"{provider}/{model}",
                        )
                models = values.get("models") or []
                if isinstance(models, list):
                    for index, model in enumerate(models):
                        if isinstance(model, dict):
                            add(
                                routes,
                                f"{display(models_path)}:providers.{provider}.models[{index}].id",
                                f"{provider}/{model.get('id', '')}",
                            )
    except (OSError, json.JSONDecodeError) as exc:
        config_errors.append(f"{display(models_path)}: {type(exc).__name__}")

agents_dir = agent_dir / "agents"
if agents_dir.is_dir():
    for path in sorted(agents_dir.glob("*.md")):
        try:
            text = path.read_text()
        except OSError as exc:
            config_errors.append(f"{display(path)}: {type(exc).__name__}")
            continue
        frontmatter = text.split("---", 2)
        if len(frontmatter) < 3:
            continue
        match = re.search(r"^model:\s*(\S+)\s*$", frontmatter[1], re.MULTILINE)
        if match:
            add(routes, f"{display(path)}:model", match.group(1))

model_tiers = repo / "common" / "model-tiers.toml"
if model_tiers.exists():
    try:
        tiers = tomllib.loads(model_tiers.read_text())
        for name, values in tiers.items():
            if isinstance(values, dict):
                add(routes, f"{model_tiers}:{name}.pi_model", values.get("pi_model"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        config_errors.append(f"{model_tiers}: {type(exc).__name__}")

unresolved: list[tuple[str, str]] = []
for source, route in routes:
    if route in {"inherit", "auto"}:
        continue
    if any(ch in route for ch in "*?["):
        if any(fnmatch.fnmatch(candidate, route) for candidate in available):
            continue
    elif route in available:
        continue
    unresolved.append((source, route))

if unresolved or config_errors:
    print("MODEL ROUTE CHECK: unresolved provider/model routes:")
    for source, route in sorted(set(unresolved)):
        print(f"- {source} -> {route}")
    for error in sorted(set(config_errors)):
        print(f"- {error}")
    print("Run `pi --list-models` and update the named declarative route; historical compatibility code is not scanned.")
    raise SystemExit(1 if strict else 0)
PY
