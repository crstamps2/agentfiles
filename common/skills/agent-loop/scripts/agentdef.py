"""Read a rendered pi agent definition and turn it into a `pi -p` command line."""
from __future__ import annotations
import dataclasses
import pathlib

WORKER_TOOLS = {"read", "grep", "find", "ls", "bash", "edit", "write"}


class AgentDefError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class AgentDef:
    name: str
    model: str
    thinking: str
    tools: list
    body: str
    fallback_models: list


def _split_frontmatter(text: str):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AgentDefError("agent file has no frontmatter")
    try:
        end = next(i for i,l in enumerate(lines[1:],1) if l.strip()=="---")
    except StopIteration:
        raise AgentDefError("unterminated frontmatter")
    fm = {}
    for l in lines[1:end]:
        if ":" in l:
            k, v = l.split(":", 1)
            fm[k.strip()] = v.strip()
    body = "\n".join(lines[end + 1:]).strip() + "\n"
    return fm, body


def _list(v: str) -> list:
    return [x.strip() for x in v.split(",") if x.strip()] if v else []


def load(pi_agents_dir, name: str) -> AgentDef:
    p = pathlib.Path(pi_agents_dir) / f"{name}.md"
    if not p.exists():
        raise AgentDefError(f"no agent definition at {p}")
    fm, body = _split_frontmatter(p.read_text())
    for k in ("model", "thinking"):
        if k not in fm:
            raise AgentDefError(f"{p}: frontmatter missing {k}")
    return AgentDef(name=fm.get("name", name), model=fm["model"], thinking=fm["thinking"],
                    tools=_list(fm.get("tools", "")), body=body,
                    fallback_models=_list(fm.get("fallbackModels", "")))


def assert_worker_safe(a: AgentDef) -> None:
    if a.fallback_models:
        # pi-subagents 0.68 removed the field entirely; for the loop it was always forbidden on workers
        # (the runner owns escalation). Keep the check so a stray field is caught before launch.
        raise AgentDefError(f"{a.name}: worker definitions must not declare fallbackModels (got {a.fallback_models}); the runner owns escalation")
    extra = sorted(set(a.tools) - WORKER_TOOLS)
    if extra:
        raise AgentDefError(f"{a.name}: worker tools outside allowlist: {extra}")
    if not a.tools:
        raise AgentDefError(f"{a.name}: worker must declare an explicit tools allowlist")


def pi_argv(a: AgentDef, prompt_file, session_dir, body_file) -> list:
    return ["pi", "-p",
            "--model", a.model,
            "--thinking", a.thinking,
            "--tools", ",".join(a.tools),
            "--append-system-prompt", str(body_file),
            "--session-dir", str(session_dir),
            "--no-prompt-templates",
            f"@{prompt_file}"]
