"""Provider health for every agent the loop launches.

When a provider reports a usage limit, record it with its reset time (`Try again in ~8405 min`) in
`<state_root>/providers.json`. Every launch then resolves its model through `resolve()`: while the
model's provider is exhausted it runs the other vendor's equivalent from the start, instead of failing
and waiting out a cool-off per agent (2026-10-08: a ChatGPT plan limit stalled every Codex-backed role).
The pi `failover` extension reads the same file for the interactive panes.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import time

# Equivalent model on the other vendor, per direction. Only models a ChatGPT-account Codex login accepts.
EQUIVALENT = {
    "openai/gpt-5.6-sol": "anthropic/claude-opus-5-5",
    "openai/gpt-6.1-sol": "anthropic/claude-opus-5-5",
    "openai/gpt-6-astra": "anthropic/claude-fable-5-1",
    "openai/gpt-5.6-terra": "anthropic/claude-sonnet-5-5",
    "openai/gpt-6-luna": "anthropic/claude-haiku-4-5",
    "anthropic/claude-opus-5-5": "openai/gpt-5.6-sol",
    "anthropic/claude-fable-5-1": "openai/gpt-6-astra",
    "anthropic/claude-sonnet-5-5": "openai/gpt-5.6-terra",
    "anthropic/claude-haiku-4-5": "openai/gpt-6-luna",
    # Existing processes can still hold pre-upgrade model IDs during this rollout.
    "openai-codex/gpt-5.6-sol": "anthropic/claude-opus-5-5",
    "openai-codex/gpt-6-astra": "anthropic/claude-fable-5-1",
    "openai-codex/gpt-5.6-terra": "anthropic/claude-sonnet-5-5",
    "openai-codex/gpt-6-luna": "anthropic/claude-haiku-4-5",
}
DEFAULT_COOL_OFF_S = 30 * 60
LIMIT = re.compile(r"usage limit|insufficient_quota|rate_limit_error|quota exceeded|\b429\b", re.I)
RESET = re.compile(r"try again in ~?\s*(\d+)\s*(min|minute|hour|h\b|sec|s\b)", re.I)


def _file(state_root) -> pathlib.Path:
    return pathlib.Path(state_root) / "providers.json"


def _load(state_root) -> dict:
    try:
        return json.loads(_file(state_root).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def provider_of(model: str) -> str:
    return (model or "").split("/", 1)[0]


def exhausted_provider(text: str, model: str) -> str | None:
    """The provider that refused, if `text` (agent stderr/stdout) reports a usage limit."""
    return provider_of(model) if model and LIMIT.search(text or "") else None


def mark_exhausted(state_root, provider: str, message: str, now: float | None = None) -> float:
    now = time.time() if now is None else now
    m = RESET.search(message or "")
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        secs = n * (3600 if unit.startswith("h") else 1 if unit.startswith("s") else 60)
    else:
        secs = DEFAULT_COOL_OFF_S
    data = _load(state_root)
    until = max(now + secs, data.get(provider, {}).get("until", 0))
    data[provider] = {"until": until, "message": (message or "")[:300], "marked": now}
    p = _file(state_root); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(data, indent=2)); os.replace(tmp, p)
    return until


def mark_healthy(state_root, provider: str) -> bool:
    """Forget a stale exhaustion mark after credentials or account capacity change."""
    data = _load(state_root)
    aliases = {provider}
    if provider in {"openai", "openai-codex"}:
        aliases |= {"openai", "openai-codex"}
    removed = aliases & data.keys()
    if not removed:
        return False
    for name in removed:
        del data[name]
    p = _file(state_root); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(data, indent=2)); os.replace(tmp, p)
    return True


def is_exhausted(state_root, provider: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return _load(state_root).get(provider, {}).get("until", 0) > now


def resolve(state_root, model: str) -> str:
    """The model to launch: the equivalent on the other vendor while this one's provider is exhausted."""
    alt = EQUIVALENT.get(model)
    if alt and is_exhausted(state_root, provider_of(model)) and not is_exhausted(state_root, provider_of(alt)):
        return alt
    return model


def note_run(state_root, model: str, stage_dir) -> str | None:
    """After an agent ran: if its logs report a usage limit, mark the provider. Returns the provider."""
    text = ""
    for name in ("stderr.log",):                   # stdout is the model's own prose, which may discuss limits
        f = pathlib.Path(stage_dir) / name
        if f.exists():
            text += f.read_text(errors="replace")[-6000:]
    prov = exhausted_provider(text, model)
    if prov:
        m = LIMIT.search(text); start = max(0, m.start() - 200) if m else 0
        mark_exhausted(state_root, prov, text[start:start + 400])
    return prov
