"""Local Orca mechanics. No admission, lifecycle decisions or ownership changes."""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time

from workspaces import Adapter, DefiniteFailure, Snapshot, WorkspaceError

REQUIRED_CAPABILITIES = frozenset((
    "worktree.create.exact-name.v1", "agent.launch.surface.v1", "agent.launch.prompt-carry.v1",
    "agent.launch.replay.v1", "agent.launch.local-presentation.v1",
    "agent-session.structured.registered-agents.v1", "agent-session.structured.pi-dialogs.v1",
))


def port_in_use(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class Backend(Adapter):
    def __init__(self, cfg, *, run, checkpoint):
        super().__init__(cfg, run=run, checkpoint=checkpoint)
        selected = os.environ.get("ORCA_CLI_COMMAND")
        if selected is None:
            name = "orca-dev" if os.environ.get("ORCA_DEV_REPO_ROOT") else ("orca-ide" if sys.platform.startswith("linux") else "orca")
            selected = shutil.which(name) or name
        self.binary = selected

    def command(self, args, *, timeout=120):
        argv = [self.binary, *args, "--environment", "local", "--json"]
        try:
            result = self.run(argv, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError("orca-unconfirmed", type(exc).__name__) from exc
        try:
            envelope = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise WorkspaceError("orca-invalid-response") from exc
        if not isinstance(envelope, dict) or envelope.get("ok") is not True or result.returncode:
            raise WorkspaceError("orca-refused", str(envelope.get("error", {}).get("code", "unknown")) if isinstance(envelope, dict) else "invalid envelope")
        data = envelope.get("result")
        if not isinstance(data, dict):
            raise WorkspaceError("orca-invalid-response", "Missing result object")
        return data

    @staticmethod
    def obj(result, field):
        value = result.get(field)
        if not isinstance(value, dict):
            raise WorkspaceError("orca-invalid-response", field)
        return value

    def git(self, *args, cwd=None):
        try:
            result = self.run(["git", "-C", str(cwd or self.cfg.workspace_repo), *args], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError("workspace-git-unconfirmed", type(exc).__name__) from exc
        if result.returncode:
            raise WorkspaceError("workspace-git-failed", str(args[0]))
        return result.stdout.strip()

    @staticmethod
    def selector(binding):
        return f"id:{binding.workspace_id}"

    def prepare(self, binding):
        status = self.command(["status"])
        runtime = self.obj(status, "runtime")
        if runtime.get("reachable") is not True or self.obj(status, "graph").get("state") != "ready":
            raise WorkspaceError("orca-not-ready")
        if REQUIRED_CAPABILITIES - set(runtime.get("capabilities", [])):
            raise WorkspaceError("orca-chat-unsupported", "Required runtime capabilities unavailable")
        repo = self.obj(self.command(["repo", "show", "--repo", f"path:{self.cfg.workspace_repo}"]), "repo")
        if (repo.get("kind") != "git" or not repo.get("id")
                or pathlib.Path(repo.get("path", "")).resolve() != self.cfg.workspace_repo.resolve()):
            raise WorkspaceError("orca-repo-mismatch")
        if binding.repo_id and binding.repo_id != repo["id"]:
            raise WorkspaceError("orca-repo-mismatch")
        binding.repo_id = repo["id"]
        self.checkpoint(binding)

    def validate_worktree(self, binding, row, *, git_check=True):
        path = row.get("path")
        branch = row.get("branch", "").removeprefix("refs/heads/")
        if (not isinstance(path, str) or not pathlib.Path(path).is_absolute() or not pathlib.Path(path).is_dir()
                or row.get("hostId") != "local" or row.get("repoId") != binding.repo_id or row.get("isMainWorktree") is not False
                or row.get("id") != f"{binding.repo_id}::{path}" or branch != binding.branch
                or (binding.worktree and pathlib.Path(binding.worktree).resolve() != pathlib.Path(path).resolve())
                or (binding.workspace_id and binding.workspace_id != row["id"])):
            raise WorkspaceError("orca-worktree-mismatch")
        if git_check and self.git("branch", "--show-current", cwd=path) != binding.branch:
            raise WorkspaceError("orca-branch-mismatch")
        if git_check and self.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path) != self.git("rev-parse", "--path-format=absolute", "--git-common-dir"):
            raise WorkspaceError("orca-repo-mismatch")
        identity = self.obj(row, "identity").get("key")
        if not isinstance(identity, str) or not identity:
            raise WorkspaceError("orca-identity-unconfirmed")
        return {"workspace_id": row["id"], "worktree": path, "identity": identity}

    def create(self, binding):
        def create():
            name = binding.key.lower()
            self.git("check-ref-format", "--branch", binding.branch)
            refs = self.git("for-each-ref", "--format=%(refname)", "refs/heads/", "refs/remotes/origin/").splitlines()
            if any(ref in refs for ref in (f"refs/heads/{binding.branch}", f"refs/remotes/origin/{binding.branch}")) or self.git("ls-remote", "--heads", "origin", f"refs/heads/{binding.branch}"):
                raise WorkspaceError("workspace-collision", "Branch exists")
            paths = self.git("worktree", "list", "--porcelain", "-z").split("\0")
            if any(pathlib.Path(p[9:]).name == name for p in paths if p.startswith("worktree ")):
                raise WorkspaceError("workspace-collision", "Ticket worktree already exists")
            self.git("fetch", "origin", "main")
            row = self.obj(self.command(["worktree", "create", "--repo", f"id:{binding.repo_id}", "--name", name,
                                         "--branch", binding.branch, "--base-branch", "origin/main", "--no-parent", "--setup", "skip"]), "worktree")
            return self.validate_worktree(binding, row)
        value = self.operation(binding, "create", [binding.repo_id, binding.key, binding.branch], create)
        binding.workspace_id, binding.worktree = value["workspace_id"], value["worktree"]
        self.checkpoint(binding)

    def workspace(self, binding, *, git_check=True):
        row = self.obj(self.command(["worktree", "show", "--worktree", self.selector(binding)]), "worktree")
        value = self.validate_worktree(binding, row, git_check=git_check)
        recorded = (binding.operations.get("create", {}).get("result", {}).get("identity")
                    or binding.operations.get("adopt", {}).get("result", {}).get("identity"))
        if recorded and value["identity"] != recorded:
            raise WorkspaceError("orca-identity-changed")
        return row, value

    def inspect(self, binding):
        row, value = self.workspace(binding)
        current = row.get("workspaceStatus")
        lane = "in-progress" if current == self.cfg.workspace_statuses.get("in_progress", "in-progress") else "other"
        if current == self.cfg.workspace_statuses.get("ready_for_review"):
            lane = "ready-for-review"
        return Snapshot(binding, value["identity"], lane)

    def verify_resources(self, binding):
        _, value = self.workspace(binding)
        handles = set(binding.terminals.values())
        dev = binding.operations.get("dev", {}).get("result", {}).get("handle")
        if dev:
            handles.add(dev)
        agent = binding.agent or binding.operations.get("agent", {}).get("result", {}).get("agent", {})
        if agent.get("kind") == "terminal":
            handles.add(agent["handle"])
        if handles:
            result = self.command(["terminal", "list", "--worktree", self.selector(binding)])
            terminals = result.get("terminals")
            if result.get("truncated") is not False or not isinstance(terminals, list):
                raise WorkspaceError("orca-terminals-unconfirmed")
            for handle in handles:
                matches = [t for t in terminals if isinstance(t, dict) and t.get("handle") == handle
                           and t.get("worktreeId") == binding.workspace_id and t.get("executionHostId") == "local" and t.get("orphaned") is not True]
                if len(matches) != 1:
                    raise WorkspaceError("orca-terminal-stale")
                if handle == agent.get("handle"):
                    for name, native in (("pty_id", "ptyId"), ("incarnation_id", "incarnationId")):
                        if agent.get(name) and matches[0].get(native) != agent[name]:
                            raise WorkspaceError("orca-terminal-replaced")
        if agent.get("kind") == "chat":
            inventory = self.command(["worktree", "ps", "--limit", "10000"])
            rows = inventory.get("worktrees")
            if inventory.get("truncated") is not False or not isinstance(rows, list):
                raise WorkspaceError("orca-chat-unconfirmed")
            owners = [w for w in rows if isinstance(w, dict) and w.get("worktreeId") == binding.workspace_id
                      and w.get("hostId") == "local" and w.get("identity", {}).get("key") == value["identity"]]
            pane = f"{agent['tab_id']}:{agent['session_id']}" if agent.get("tab_id") else agent["handle"]
            if len(owners) != 1 or len([a for a in owners[0].get("agents", []) if isinstance(a, dict) and a.get("paneKey") == pane
                                       and a.get("structuredHostOwned") is True and a.get("agentType") == "pi"]) != 1:
                raise WorkspaceError("orca-chat-stale")
        pages = set(binding.views.values())
        pages.update(op.get("result", {}).get("page") for stage, op in binding.operations.items()
                     if stage.startswith("view-") and op.get("status") == "done")
        pages.discard(None)
        if pages:
            tabs = self.command(["tab", "list", "--worktree", self.selector(binding)]).get("tabs")
            if not isinstance(tabs, list):
                raise WorkspaceError("orca-browser-unconfirmed")
            for page in pages:
                if len([t for t in tabs if isinstance(t, dict) and t.get("browserPageId") == page and t.get("worktreeId") == binding.workspace_id]) != 1:
                    raise WorkspaceError("orca-browser-stale")

    def observe_terminal(self, binding):
        handle = binding.agent["handle"]
        result = self.command(["terminal", "list", "--worktree", self.selector(binding)])
        rows = result.get("terminals")
        if result.get("truncated") is not False or not isinstance(rows, list):
            raise WorkspaceError("adoption-terminal-unconfirmed")
        matches = [row for row in rows if isinstance(row, dict) and row.get("handle") == handle]
        if len(matches) != 1:
            raise WorkspaceError("adoption-terminal-ambiguous-or-missing")
        row = matches[0]
        if (row.get("worktreeId") != binding.workspace_id or row.get("executionHostId") != "local"
                or row.get("orphaned") is not False or row.get("connected") is not True or row.get("writable") is not True
                or any(not isinstance(row.get(k), str) or not row[k] for k in ("ptyId", "incarnationId"))):
            raise WorkspaceError("adoption-terminal-not-live-local")
        terminal = self.obj(self.command(["terminal", "read", "--terminal", handle, "--limit", "1"]), "terminal")
        if terminal.get("handle") != handle or terminal.get("status") != "running":
            raise WorkspaceError("adoption-terminal-not-running")
        return {"pty_id": row["ptyId"], "incarnation_id": row["incarnationId"]}

    def terminal(self, binding, stage, title, command):
        def create():
            value = self.obj(self.command(["terminal", "create", "--worktree", self.selector(binding), "--title", title, "--command", command]), "terminal")
            handle = value.get("handle")
            if not isinstance(handle, str) or not handle:
                raise WorkspaceError("orca-terminal-unconfirmed")
            return {"handle": handle}
        return self.operation(binding, stage, [title, command], create)["handle"]

    def setup(self, binding):
        if binding.operations.get("setup", {}).get("status") == "done":
            return
        failed = binding.operations.get("setup", {}).get("status") == "failed"
        attempts = sum(key.startswith("setup-terminal-") for key in binding.operations)
        number = attempts if failed else max(0, attempts - 1)
        command = shlex.join(["env", f"WORKTREE_NAME={binding.key.lower()}", f"WORKTREE_ROOT_PATH={self.cfg.workspace_repo}", "bin/worktree-setup"])
        command = f"cd {shlex.quote(binding.worktree)} && exec {command}"
        binding.terminals["setup"] = self.terminal(binding, f"setup-terminal-{number}", "Setup", command)
        self.checkpoint(binding)
        value = self.obj(self.command(["terminal", "wait", "--terminal", binding.terminals["setup"], "--for", "exit", "--timeout-ms", "1800000"], timeout=1815), "wait")
        if value.get("satisfied") is not True or type(value.get("exitCode")) is not int:
            binding.phase = "uncertain"
            self.checkpoint(binding)
            raise WorkspaceError("setup-unconfirmed")
        binding.operations["setup"] = {"status": "done" if value["exitCode"] == 0 else "failed", "exit_code": value["exitCode"]}
        self.checkpoint(binding)
        if value["exitCode"] != 0:
            raise DefiniteFailure("setup-failed")

    def server_ready(self, binding, *, timeout=120):
        deadline = time.monotonic() + timeout
        hostname = f"admin.{binding.key.lower()}.test"
        while time.monotonic() < deadline:
            try:
                status = self.run(["overmind", "status"], cwd=binding.worktree,
                                  env={**os.environ, "OVERMIND_SOCKET": str(pathlib.Path(binding.worktree) / ".overmind.sock")},
                                  capture_output=True, text=True, timeout=10)
                web = any(len(parts := line.split()) >= 3 and parts[0] == "web" and parts[2] == "running" for line in status.stdout.splitlines())
                if status.returncode == 0 and web:
                    response = self.run(["curl", "-q", "--insecure", "--silent", "--output", os.devnull, "--write-out", "%{http_code}",
                                         "--max-time", "5", "--noproxy", "*", "--resolve", f"{hostname}:443:127.0.0.1", f"https://{hostname}"],
                                        capture_output=True, text=True, timeout=7)
                    if response.returncode == 0 and re.fullmatch(r"[23][0-9]{2}", response.stdout.strip()):
                        return True
            except (OSError, subprocess.TimeoutExpired):
                return False
            time.sleep(2)
        return False

    def start_dev(self, binding):
        recorded = binding.operations.get("dev", {})
        if recorded.get("status") == "done":
            binding.terminals["dev"] = recorded["result"]["handle"]
            self.checkpoint(binding)
        if "dev" not in binding.terminals:
            try:
                match = re.search(r"^PORT=([0-9]{1,5})\s*$", (pathlib.Path(binding.worktree) / ".env").read_text(), re.MULTILINE)
                port = int(match[1]) if match else 0
            except (OSError, ValueError) as exc:
                raise DefiniteFailure("port-unconfigured") from exc
            if not 1 <= port <= 65535:
                raise DefiniteFailure("port-unconfigured")
            if port_in_use(port):
                raise DefiniteFailure("port-conflict")
            command = f"cd {shlex.quote(binding.worktree)} && exec bin/dev"
            binding.terminals["dev"] = self.terminal(binding, "dev", "Dev", command)
            self.checkpoint(binding)
        ready = self.server_ready(binding)
        binding.operations["serving"] = {"status": "done", "result": {"ready": ready}}
        self.checkpoint(binding)

    def start_agent(self, binding, prompt):
        def launch():
            result = self.command(["agent", "launch", "--worktree", self.selector(binding), "--agent", "pi", "--surface", "chat", "--prompt", prompt])
            outcome = self.obj(result, "outcome")
            delivery = self.obj(result, "prompt")
            receipt = self.obj(result, "receipt")
            kind, handle = outcome.get("kind"), outcome.get("handle")
            proof, message = delivery.get("outcome"), delivery.get("messageId")
            if (result.get("worktreeId") != binding.workspace_id or kind not in ("structured", "terminal")
                    or not isinstance(handle, str) or not handle or delivery.get("delivery") != "submit"
                    or receipt.get("mode") != kind or receipt.get("preferred") != "structured"
                    or not isinstance(receipt.get("reason"), str) or not isinstance(receipt.get("detail"), str)
                    or proof not in ("journaled", "handed-to-terminal")
                    or (proof == "journaled" and (not isinstance(message, str) or not message))
                    or (kind == "structured" and proof != "journaled")):
                raise WorkspaceError("orca-agent-unconfirmed")
            agent = {"kind": "chat" if kind == "structured" else "terminal", "handle": handle}
            if kind == "structured":
                session = outcome.get("sessionId")
                if not isinstance(session, str) or not session:
                    raise WorkspaceError("orca-chat-session-unconfirmed")
                agent["session_id"] = session
                tab = outcome.get("tabId")
                if isinstance(tab, str) and tab:
                    agent["tab_id"] = tab
            return {"agent": agent, "operation_id": result.get("operationId"), "detail": receipt["detail"],
                    "prompt": {"delivery": "submit", "outcome": proof, "message_id": message if proof == "journaled" else None}}
        value = self.operation(binding, "agent", ["pi", "chat", prompt], launch)
        binding.agent = value["agent"]
        self.checkpoint(binding)

    def ensure_views(self, binding, ref_url):
        self.operation(binding, "reference", [ref_url], lambda: self.command(["reference", "add", ref_url, "--worktree", self.selector(binding)]))
        urls = {"reference": ref_url}
        if binding.operations.get("serving", {}).get("result", {}).get("ready"):
            urls["app"] = f"https://admin.{binding.key.lower()}.test"
        for role, url in urls.items():
            def create(url=url):
                result = self.command(["tab", "create", "--worktree", self.selector(binding), "--url", url])
                page = result.get("browserPageId")
                if not isinstance(page, str) or not page:
                    raise WorkspaceError("orca-browser-unconfirmed")
                return {"page": page}
            binding.views[role] = self.operation(binding, f"view-{role}", [url], create)["page"]
            self.checkpoint(binding)

    def present(self, binding, value):
        row, _ = self.workspace(binding)
        previous = row.get("comment") or ""
        if not isinstance(previous, str):
            raise WorkspaceError("orca-comment-unconfirmed")
        line = "agent-loop: " + ("working · " if value.spinner else "") + value.text
        pattern = re.compile(r"(?m)^agent-loop: [^\r\n]*$")
        if pattern.search(previous):
            comment = pattern.sub(lambda _: line, previous)
        else:
            comment = previous + ("\n" if previous and not previous.endswith("\n") else "") + line
        if previous == comment:
            return
        result = self.obj(self.command(["worktree", "set", "--worktree", self.selector(binding), "--comment", comment]), "worktree")
        self.validate_worktree(binding, result)
        if result.get("comment") != comment:
            raise WorkspaceError("orca-comment-unconfirmed")

    def move_to_review(self, binding, expected):
        destination = self.cfg.workspace_statuses.get("ready_for_review")
        if not destination or expected.lane != "in-progress":
            return False
        current = self.inspect(binding)
        if current.identity != expected.identity or current.lane != "in-progress" or not self.before_move():
            return False
        current = self.inspect(binding)
        if current.identity != expected.identity or current.lane != "in-progress":
            return False
        row = self.obj(self.command(["worktree", "set", "--worktree", self.selector(binding), "--workspace-status", destination]), "worktree")
        value = self.validate_worktree(binding, row)
        if value["identity"] != expected.identity or row.get("workspaceStatus") != destination:
            raise WorkspaceError("orca-review-move-unconfirmed")
        return True

    def notify(self, binding, title, body):
        row, _ = self.workspace(binding, git_check=False)
        if row.get("isUnread") is True:
            return
        result = self.obj(self.command(["worktree", "set", "--worktree", self.selector(binding), "--unread"]), "worktree")
        self.validate_worktree(binding, result, git_check=False)
        if result.get("isUnread") is not True:
            raise WorkspaceError("orca-notification-unconfirmed")
