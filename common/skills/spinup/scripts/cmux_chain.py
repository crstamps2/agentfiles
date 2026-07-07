"""cmux spin-up chain. Self-contained CLI; imports shared deterministic helpers from spinup_helper.
Adapt the constants at the top of this file (MAIN_REPO, WORKTREE_BASE, GH_REPO, JIRA_BROWSE_BASE)
to match your project before use."""

import argparse
import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import spinup_helper as sh

PR_NOTIFY_STATE_PATH = Path.home() / ".claude" / "cache" / "cmux-pr-notify.json"

MAIN_REPO = Path.home() / "workspace" / "your-app"          # TODO: set to your prime checkout path
WORKTREE_BASE = Path.home() / "workspace" / "your-app-worktrees"  # TODO: set to your worktree base
CMUX = "cmux"
GH_REPO = "your-org/your-app"                                # TODO: set to your GitHub repo
JIRA_BROWSE_BASE = "https://your-jira-instance/browse"   # TODO: set to your Jira base URL
SETUP_TIMEOUT_S = 600     # bundle/yarn can take minutes
SERVING_TIMEOUT_S = 120
READY_TIMEOUT_S = 30      # max seconds to wait for Claude's TUI input box to render
SUBMIT_VERIFY_TIMEOUT_S = 12  # max seconds to wait for a new transcript file confirming submission

_WS_RE = re.compile(r"workspace:\d+")
_SURF_RE = re.compile(r"surface:\d+")
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[\]P].*?(?:\x07|\x1b\\)|\x1b[=>]', re.S)

# Group definitions for workspace classification.
GROUP_IN_PROGRESS = {"name": "In Progress", "hex": "#F59E0B", "symbol": "hammer.fill"}
GROUP_PR_REVIEWS  = {"name": "Pull Request Reviews", "hex": "#3B82F6", "symbol": "arrow.triangle.pull"}
GROUP_ARCHIVE     = {"name": "Archive", "hex": "#6B7280", "symbol": "archivebox.fill"}

WORKSPACE_MAP_PATH = Path.home() / ".claude" / "cache" / "cmux-workspace-map.json"


class CmuxError(RuntimeError):
    pass


class GitError(RuntimeError):
    pass


class TeardownRefused(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Task 1: cmux + git subprocess wrappers
# ---------------------------------------------------------------------------

def _cmux(args: list) -> str:
    """Run a cmux command; return stdout. Raise CmuxError on failure."""
    try:
        r = subprocess.run([CMUX, *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise CmuxError(f"{CMUX} not found on PATH")
    if r.returncode != 0:
        raise CmuxError(f"cmux {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def _first(pattern, text, what):
    m = pattern.search(text)
    if not m:
        raise CmuxError(f"could not parse {what} from cmux output: {text!r}")
    return m.group(0)


def cmux_new_workspace(cwd: str, command: str, name: str) -> str:
    out = _cmux(["new-workspace", "--cwd", str(cwd), "--command", command, "--name", name])
    return _first(_WS_RE, out, "workspace ref")


def cmux_new_surface(workspace: str, surface_type: str = "terminal", url: str = None) -> str:
    args = ["new-surface", "--workspace", workspace, "--type", surface_type]
    if url:
        args += ["--url", url]
    out = _cmux(args)
    return _first(_SURF_RE, out, "surface ref")


def _send_with_wake(args: list, timeout_s: float = 8.0, delay_s: float = 0.5) -> None:
    """Run a `send`/`send-key` against a terminal surface, retrying through cmux backend
    hibernation.

    cmux instantiates a terminal's PTY/Ghostty backend lazily and hibernates it
    (ghostty=nil) when the surface isn't actively rendering; `send`/`send-key` then fail
    with "Surface is not a terminal" until the backend is woken. `refresh-surfaces` wakes
    it, but the backend flickers sub-second, so a pre-check can pass and the next send
    still lose the race. Retry the actual send, nudging with refresh-surfaces between
    attempts, until it lands (or the short timeout elapses). The happy path -- backend
    already awake -- sends once with no refresh and no delay. A non-hibernation CmuxError
    is re-raised immediately.

    HARD LIMITATION (verified 2026-06-09): this only rescues the sub-second backend flicker
    while the cmux *window is actively rendering the target workspace*. cmux services
    send-able terminal backends ONLY for the workspace it currently displays; a surface in
    a non-displayed workspace is not send-able and CANNOT be woken from the socket --
    `select-workspace` does not override a user actively viewing another workspace, so a
    background/contended spinup will still fail here. The timeout is kept short so that
    unwinnable case fails fast instead of hanging. See memory project_spinup_cmux_failure_modes."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            _cmux(args)
            return
        except CmuxError as e:
            if "not a terminal" not in str(e).lower() or time.monotonic() >= deadline:
                raise
            try:
                _cmux(["refresh-surfaces"])
            except CmuxError:
                pass
            time.sleep(delay_s)


def cmux_send(surface: str, text: str) -> None:
    _send_with_wake(["send", "--surface", surface, text])


def cmux_send_key(surface: str, key: str) -> None:
    _send_with_wake(["send-key", "--surface", surface, key])


def cmux_focus_workspace(workspace: str) -> None:
    """Bring `workspace` to the foreground. cmux boots a terminal's shell (its Ghostty
    backend) lazily -- only when the terminal's workspace is visible. `new-workspace`
    auto-focuses, so the setup tab spawns fine, but the chain then sits in wait_for_setup
    (and later wait_for_dev_server) for up to minutes, during which focus can drift to
    another workspace (e.g. a background-launched spinup running behind an active
    workspace). Any terminal created via new-surface while the workspace is backgrounded
    stays a hollow, shell-less surface, and the immediate send fails with
    "Surface is not a terminal". Re-asserting focus before each terminal-creating step
    keeps the chain robust regardless of what holds foreground. Best-effort."""
    try:
        _cmux(["select-workspace", "--workspace", workspace])
    except CmuxError:
        pass


def cmux_notify(title: str, body: str, workspace: str = None) -> None:
    args = ["notify", "--title", title, "--body", body]
    if workspace:
        args += ["--workspace", workspace]
    try:
        _cmux(args)
    except CmuxError:
        pass  # notification is best-effort


# ---------------------------------------------------------------------------
# Group management helpers
# ---------------------------------------------------------------------------

def cmux_group_list() -> list:
    """Return the list of group dicts from workspace.group.list. Returns [] on any error."""
    try:
        out = _cmux(["rpc", "workspace.group.list", "{}"])
        data = json.loads(out)
        return data.get("groups", [])
    except (CmuxError, json.JSONDecodeError, AttributeError):
        return []


def cmux_group_ensure(name: str, hex: str, symbol: str) -> str:
    """Return the group_id for a group named `name`, creating it (with color/icon/pin)
    if it does not yet exist."""
    for g in cmux_group_list():
        if g.get("name") == name:
            return g["id"]
    # Group not found -- create it.
    # NOTE: workspace.group.create has a side effect: it also spawns a blank anchor workspace
    # and pulls in the currently-selected workspace. We still explicitly call add after this
    # (idempotent) so the intended workspace ends up in the group regardless.
    create_payload = json.dumps({"name": name})
    resp_raw = _cmux(["rpc", "workspace.group.create", create_payload])
    resp = json.loads(resp_raw)
    group_id = resp["group"]["id"]
    _cmux(["rpc", "workspace.group.set_color", json.dumps({"group_id": group_id, "hex": hex})])
    _cmux(["rpc", "workspace.group.set_icon", json.dumps({"group_id": group_id, "symbol": symbol})])
    _cmux(["rpc", "workspace.group.pin", json.dumps({"group_id": group_id})])
    return group_id


def cmux_group_add(group_id: str, workspace: str) -> None:
    """Assign `workspace` to the group identified by `group_id`.

    `workspace` may be a ref ('workspace:N') or a UUID -- the workspace.group.add
    RPC accepts either form for workspace_id and resolves refs itself (verified live
    2026-06-05), so no manual ref->UUID resolution is needed."""
    _cmux(["rpc", "workspace.group.add", json.dumps({"group_id": group_id, "workspace_id": workspace})])


def assign_to_group(workspace_ref: str, group_spec: dict) -> None:
    """Best-effort: ensure the group exists and add `workspace_ref` to it.
    Logs a warning to stderr on any failure; never raises."""
    try:
        group_id = cmux_group_ensure(group_spec["name"], group_spec["hex"], group_spec["symbol"])
        cmux_group_add(group_id, workspace_ref)
    except Exception as e:
        print(f"warning: workspace group assignment failed ({e})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Teardown guards and helpers
# ---------------------------------------------------------------------------

def worktree_path_guard(name: str) -> Path:
    """Resolve <name> to a worktree dir under WORKTREE_BASE, or raise TeardownRefused.

    Refuses empty/dotted names, anything with a path separator, the prime checkout
    (MAIN_REPO), any resolved path outside WORKTREE_BASE, and nonexistent dirs."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise TeardownRefused(f"invalid worktree name: {name!r}")
    base = WORKTREE_BASE.resolve()
    path = (WORKTREE_BASE / name).resolve()
    if base not in path.parents:
        raise TeardownRefused(f"{path} is not under {base}; refusing")
    if path == MAIN_REPO.resolve():
        raise TeardownRefused("refusing to tear down the prime checkout")
    if not path.is_dir():
        raise TeardownRefused(f"worktree does not exist: {path}")
    return path


def worktree_archive(name: str) -> None:
    """Run bin/worktree-teardown (drops the worktree's DB/Redis/ES, frees its slot).
    Scoped by worktree name; never touches the prime DB.
    cwd=MAIN_REPO matches the previous conductor-archive behavior: _teardown.sh resolves
    its lib files via $0 (script path), not $PWD, so cwd is irrelevant to script lookup."""
    env = {**os.environ,
           "WORKTREE_NAME": name,
           "WORKTREE_ROOT_PATH": str(MAIN_REPO)}
    r = subprocess.run(["bin/worktree-teardown"], cwd=str(MAIN_REPO),
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise CmuxError(f"worktree-teardown failed: {r.stderr.strip() or r.stdout.strip()}")


def git_worktree_remove_force(path: Path) -> None:
    """Remove the worktree dir with --force (keeps the branch ref).

    If the directory still exists after git worktree remove (e.g. terminals re-created
    files during teardown), shutil.rmtree it and prune the worktree list as a backstop."""
    r = subprocess.run(["git", "-C", str(MAIN_REPO), "worktree", "remove", "--force", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise GitError(f"git worktree remove failed: {r.stderr.strip()}")
    if os.path.isdir(str(path)):
        shutil.rmtree(str(path), ignore_errors=True)
        subprocess.run(["git", "-C", str(MAIN_REPO), "worktree", "prune"],
                       capture_output=True, text=True)


def cmux_close_workspace(ref: str) -> None:
    """Best-effort close a workspace by ref. (Task 0 confirmed the --workspace flag.)"""
    try:
        _cmux(["workspace", "close", "--workspace", ref])
    except CmuxError:
        pass


# ---------------------------------------------------------------------------
# Workspace ref/name resolution + spin-up map
# ---------------------------------------------------------------------------

def _iter_workspace_list(both: bool = False):
    """Yield (ref, uuid_or_None, name) for each workspace. Robust to the '*' selected
    marker and indentation: locate the workspace:N token, not a fixed column."""
    flags = ["workspace", "list"] + (["--id-format", "both"] if both else [])
    try:
        out = _cmux(flags)
    except CmuxError:
        return
    for line in out.splitlines():
        toks = line.replace("*", " ").split()
        for i, t in enumerate(toks):
            if _WS_RE.fullmatch(t):
                rest = toks[i + 1:]
                if both and rest and re.fullmatch(r"[0-9A-Fa-f-]{36}", rest[0]):
                    uuid, name_toks = rest[0], rest[1:]
                else:
                    uuid, name_toks = None, rest
                name = name_toks[0] if name_toks else None
                yield t, uuid, name
                break


def workspace_ref_for_name(name: str):
    for ref, _uuid, nm in _iter_workspace_list():
        if nm == name:
            return ref
    return None


def workspace_name_for_ref(ref: str):
    for r, _uuid, nm in _iter_workspace_list():
        if r == ref:
            return nm
    return None


def workspace_uuid_for_ref(ref: str):
    for r, uuid, _nm in _iter_workspace_list(both=True):
        if r == ref:
            return uuid
    return None


def load_workspace_map() -> dict:
    try:
        return json.loads(WORKSPACE_MAP_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_workspace_map(m: dict) -> None:
    WORKSPACE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    WORKSPACE_MAP_PATH.write_text(json.dumps(m, indent=2))


def record_workspace_map(workspace_ref: str, worktree_name: str) -> None:
    uuid = workspace_uuid_for_ref(workspace_ref)
    if uuid:
        m = load_workspace_map()
        m[uuid] = worktree_name
        save_workspace_map(m)


def prune_workspace_map(worktree_name: str) -> None:
    m = load_workspace_map()
    pruned = {k: v for k, v in m.items() if v != worktree_name}
    if pruned != m:
        save_workspace_map(pruned)


def teardown_worktree(name: str, *, workspace_ref: str = None,
                      close_workspace: bool = True) -> None:
    """Fully tear down a worktree: close the workspace FIRST (so terminals stop writing),
    then archive external resources, force-remove the worktree (keeps the branch), notify.

    Close-first order prevents open terminals from re-creating app/tmp dirs during removal,
    which was the root cause of stale directories blocking re-spins (proj-5864, proj-5868).

    Raises TeardownRefused (before any destructive action) if `name` is out of scope.
    Individual steps are resilient: a failure is collected and surfaced in the notification
    rather than aborting cleanup.

    When close_workspace=False (close-hook path -- workspace already gone), the close step
    is skipped entirely."""
    path = worktree_path_guard(name)   # raises TeardownRefused -> nothing destructive ran
    errors = []
    if close_workspace:
        ref = workspace_ref or workspace_ref_for_name(name)
        if ref:
            cmux_close_workspace(ref)
    try:
        worktree_archive(name)
    except Exception as e:
        errors.append(f"archive: {e}")
    try:
        git_worktree_remove_force(path)
    except Exception as e:
        errors.append(f"worktree-remove: {e}")
    prune_workspace_map(name)
    if errors:
        cmux_notify(f"cmux: teardown of {name} had issues", "; ".join(errors))
    else:
        cmux_notify(f"cmux: cleaned up {name}", f"worktree {name} removed")


def focused_worktree_name():
    """Return the focused workspace's name if it is a proj-*/pr-* worktree, else None."""
    try:
        info = json.loads(_cmux(["identify", "--json"]))
    except (CmuxError, json.JSONDecodeError):
        return None
    ref = (info.get("focused") or {}).get("workspace_ref") \
        or (info.get("caller") or {}).get("workspace_ref")
    if not ref:
        return None
    name = workspace_name_for_ref(ref)
    if name and (name.startswith("proj-") or name.startswith("pr-")):
        return name
    return None


def cmd_spindown(args) -> int:
    name = getattr(args, "name", None)
    if not name:
        name = focused_worktree_name()
        if not name:
            cmux_notify("cmux: spindown", "no worktree name given and focused workspace is not a worktree")
            print("spindown: no worktree name and focused workspace is not a proj-*/pr-* worktree", file=sys.stderr)
            return 1
    try:
        teardown_worktree(name, close_workspace=True)
        return 0
    except TeardownRefused as e:
        cmux_notify(f"cmux: spindown refused for {name}", str(e))
        print(f"spindown refused: {e}", file=sys.stderr)
        return 1


def resolve_closed_worktree(event: dict):
    """Map a workspace.closed event to a worktree name. PRIMARY: payload.cwd (the worktree
    path) -- if under WORKTREE_BASE, the name is its basename. Falls back to the payload
    title, then the spin-up workspace map keyed by workspace_id. (Task 0 confirmed cwd is
    present in the frame.)"""
    payload = event.get("payload") or event.get("data") or event
    cwd = payload.get("cwd")
    if cwd:
        p = Path(cwd)
        try:
            if (WORKTREE_BASE.resolve() in p.resolve().parents
                    and p.resolve() != MAIN_REPO.resolve() and p.is_dir()):
                return p.name
        except OSError:
            pass
    name = (payload.get("title") or payload.get("custom_title")
            or payload.get("workspace_name") or payload.get("name"))
    if name and (WORKTREE_BASE / name).is_dir():
        return name
    wid = payload.get("workspace_id") or payload.get("id")
    if wid:
        return load_workspace_map().get(wid)
    return None


def handle_close_event(event: dict) -> None:
    """Tear down the worktree behind a workspace.closed event, if it is one of ours and
    in scope. The workspace is already closed, so close_workspace=False."""
    name = resolve_closed_worktree(event)
    if not name:
        return
    try:
        worktree_path_guard(name)
    except TeardownRefused:
        return
    teardown_worktree(name, close_workspace=False)


def sweep_archive_group() -> None:
    """Tear down any proj-*/pr-* worktree workspace sitting in the Archive group."""
    arch = next((g for g in cmux_group_list() if g.get("name") == GROUP_ARCHIVE["name"]), None)
    if not arch:
        return
    anchor = arch.get("anchor_workspace_ref")
    for ref in arch.get("member_workspace_refs", []):
        if ref == anchor:
            continue
        name = workspace_name_for_ref(ref)
        if not name or not (name.startswith("proj-") or name.startswith("pr-")):
            continue
        if not os.path.isdir(os.path.join(str(WORKTREE_BASE), name)):
            continue
        try:
            teardown_worktree(name, workspace_ref=ref, close_workspace=True)
        except TeardownRefused:
            pass


def cmd_close_listen(args) -> int:
    """Stream workspace.closed events and tear down the matching worktrees. Long-running;
    intended to be supervised by launchd (KeepAlive)."""
    proc = subprocess.Popen(
        [CMUX, "events", "--name", "workspace.closed", "--reconnect"],
        stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") in ("ack", "heartbeat") or event.get("heartbeat"):
            continue
        if event.get("name") != "workspace.closed":
            continue
        try:
            handle_close_event(event)
        except Exception as e:
            print(f"close-handler error: {e}", file=sys.stderr)
    return 0


def git_worktree_add(path: str, branch: str, new_branch: bool) -> None:
    args = ["git", "-C", str(MAIN_REPO), "worktree", "add"]
    if new_branch:
        args += ["-b", branch, str(path)]
    else:
        args += [str(path), branch]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise GitError(f"git worktree add failed: {r.stderr.strip()}")


# ---------------------------------------------------------------------------
# Port-conflict detection helpers
# ---------------------------------------------------------------------------

def read_worktree_port(worktree: str) -> "int | None":
    """Parse PORT= from <worktree>/.env; return the int, or None if absent/unparseable."""
    env_path = os.path.join(worktree, ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("PORT="):
                    value = line[len("PORT="):].strip()
                    return int(value)
    except (FileNotFoundError, ValueError, OSError):
        pass
    return None


def port_in_use(port: int) -> bool:
    """Return True if a TCP listener is active on the port locally."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def find_free_port(start: int, attempts: int = 200) -> int:
    """Scan start, start+1, ... up to attempts ports; return first where port is free.
    Raises CmuxError if none found within the scan window."""
    for p in range(start, start + attempts):
        if not port_in_use(p):
            return p
    raise CmuxError(f"no free port found in range {start}–{start + attempts - 1}")


def rerun_setup_with_port(worktree: str, name: str, port: int) -> bool:
    """Re-run worktree-setup forcing WORKTREE_PORT. Return True on exit 0.

    generate_env skips an existing .env (lib/env-gen.sh:58), so remove it to
    force regeneration with the new WORKTREE_PORT.  puma-dev is reconfigured
    from WORKTREE_PORT regardless of whether .env existed.
    """
    try:
        os.remove(os.path.join(worktree, ".env"))
    except FileNotFoundError:
        pass
    env = dict(os.environ)
    env["WORKTREE_PORT"] = str(port)
    env["WORKTREE_NAME"] = name
    env["WORKTREE_ROOT_PATH"] = str(MAIN_REPO)
    r = subprocess.run(["bin/worktree-setup"], cwd=worktree, env=env,
                       capture_output=True, text=True)
    return r.returncode == 0


def _proc_cwd(pid: str):
    try:
        r = subprocess.run(["lsof", "-p", pid], capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    for line in r.stdout.splitlines():
        f = line.split()
        if len(f) >= 9 and f[3] == "cwd":
            return f[-1]
    return None


def kill_worktree_port_orphans(worktree: str, port: int) -> int:
    """Kill TCP listeners on `port` whose cwd is `worktree` -- orphaned pumas left by a
    failed run. Returns the count killed. Caller should only invoke this when
    dev_server_up(worktree) is False (i.e. no live overmind owns them)."""
    try:
        r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                           capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0
    killed = 0
    wt_real = os.path.realpath(worktree)
    for pid in r.stdout.split():
        cwd = _proc_cwd(pid)
        if cwd and os.path.realpath(cwd) == wt_real:
            subprocess.run(["kill", pid], capture_output=True)
            killed += 1
    return killed


def port_holder(port: int) -> str:
    """Best-effort one-line description of what holds the port. Never raises."""
    try:
        r = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return ""
        # Second line of lsof output is the first process row (first line is header)
        lines = r.stdout.strip().splitlines()
        if len(lines) < 2:
            return ""
        # Parse the PID from the second field
        parts = lines[1].split()
        if len(parts) < 2:
            return ""
        pid = parts[1]
        # Try to find the cwd of that pid via a second lsof call
        r2 = subprocess.run(
            ["lsof", "-p", pid, "-Fn", "-dcwd"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r2.stdout.splitlines():
            if line.startswith("n") and "/" in line:
                return line[1:]  # strip the leading 'n'
        return ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Task 2: Worktree naming + creation
# ---------------------------------------------------------------------------

def worktree_name_for_ticket(ticket_key: str) -> str:
    return ticket_key.lower()


def worktree_name_for_pr(pr_number: int) -> str:
    return f"pr-{pr_number}"


def worktree_path(name: str) -> str:
    return str(WORKTREE_BASE / name)


def _ensure_base_dir() -> None:
    """Create the worktree base directory if it doesn't exist (test-injectable)."""
    WORKTREE_BASE.mkdir(parents=True, exist_ok=True)


def branch_exists(branch: str) -> bool:
    """True if a local branch with this name already exists in the main repo."""
    r = subprocess.run(
        ["git", "-C", str(MAIN_REPO), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True, text=True)
    return r.returncode == 0


def branch_has_commits(branch: str) -> bool:
    """True if `branch` has commits beyond `main` (i.e. real in-progress work).
    A branch sitting at base HEAD with 0 commits ahead is treated as fresh."""
    r = subprocess.run(
        ["git", "-C", str(MAIN_REPO), "rev-list", "--count", f"main..{branch}"],
        capture_output=True, text=True)
    try:
        return r.returncode == 0 and int(r.stdout.strip()) > 0
    except ValueError:
        return False


def ensure_worktree(name: str, branch: str, new_branch: bool) -> str:
    """Create the worktree if its path doesn't already exist; return the path.

    If new_branch is requested but the branch already exists (e.g. re-spinning a
    torn-down ticket), attach the existing branch instead of failing on `worktree add -b`."""
    _ensure_base_dir()
    path = worktree_path(name)
    if not os.path.isdir(path):
        if new_branch and branch_exists(branch):
            new_branch = False
        git_worktree_add(path, branch, new_branch)
    return path


# ---------------------------------------------------------------------------
# Task 3: Setup tab + success gate
# ---------------------------------------------------------------------------

def open_setup_tab(worktree: str, name: str) -> str:
    """Open the workspace with worktree-setup running in the first (visible) tab.
    Appends an exit sentinel so wait_for_setup can gate the next step."""
    command = (
        f"WORKTREE_NAME={name} WORKTREE_ROOT_PATH={MAIN_REPO} "
        f"bin/worktree-setup; echo EXIT:$? > {worktree}/.cmux-setup-status"
    )
    return cmux_new_workspace(worktree, command, name)


def wait_for_setup(worktree: str, timeout_s: int = SETUP_TIMEOUT_S, poll_s: float = 2.0) -> bool:
    """Poll the sentinel; True iff it reports EXIT:0 within the timeout."""
    status_file = os.path.join(worktree, ".cmux-setup-status")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with open(status_file) as f:
                content = f.read().strip()
            if content.startswith("EXIT:"):
                return content == "EXIT:0"
        except FileNotFoundError:
            pass
        time.sleep(poll_s)
    return False


# ---------------------------------------------------------------------------
# Task 4: Dev-server tab + serving gate (overmind-based)
# ---------------------------------------------------------------------------

def open_dev_server_tab(workspace: str, worktree: str) -> str:
    """Start the dev server via the repo's canonical `bin/dev` (worktree-aware: it reads
    WORKTREE_NAME from .env and picks Procfile.dev.worktree itself).

    bin/dev does not clear a stale .overmind.sock the way conductor-server did, and a
    sock left by a crashed run blocks overmind from starting -- so remove the sock first,
    but ONLY when no live overmind owns it (safer than conductor-server's unconditional rm)."""
    sock = os.path.join(worktree, ".overmind.sock")
    if os.path.exists(sock) and not dev_server_up(worktree):
        try:
            os.remove(sock)
        except OSError:
            pass
    surface = cmux_new_surface(workspace, surface_type="terminal")
    cmux_send(surface, "bin/dev")  # resilient: wakes the backend + retries
    cmux_send_key(surface, "enter")
    return surface


def test_url_for(name: str) -> str:
    return f"https://admin.{name}.test"


def dev_server_up(worktree: str) -> bool:
    """True iff overmind is managing a RUNNING web process for the worktree.
    Robust replacement for a curl check -- a stale/zombie puma on the port can
    satisfy curl, but it won't show up as an overmind-managed running web proc."""
    if not os.path.exists(os.path.join(worktree, ".overmind.sock")):
        return False
    r = subprocess.run(
        ["overmind", "status"], cwd=worktree,
        env={**os.environ, "OVERMIND_SOCKET": os.path.join(worktree, ".overmind.sock")},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "web":
            return parts[2] == "running"
    return False


def wait_for_dev_server(worktree: str, timeout_s: int = SERVING_TIMEOUT_S, poll_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if dev_server_up(worktree):
            return True
        time.sleep(poll_s)
    return False


# ---------------------------------------------------------------------------
# Surface reading + readiness pollers
# ---------------------------------------------------------------------------

def read_surface(surface: str, lines: int = 40) -> str:
    """Read the current text of a cmux pane, stripping ANSI escapes. Best-effort: returns "" on failure."""
    try:
        out = _cmux(["read-screen", "--surface", surface, "--lines", str(lines)])
    except CmuxError:
        return ""
    return _ANSI_RE.sub("", out)


def wait_for_claude_ready(surface: str, timeout_s: int = READY_TIMEOUT_S, poll_s: float = 1.0) -> bool:
    """Poll until Claude's TUI input box (the `❯` prompt) has rendered.

    Best-effort: returns True on detect, False on timeout. Callers proceed
    regardless because the transcript-retry loop is the backstop."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if "❯" in read_surface(surface):
            return True
        time.sleep(poll_s)
    return False


def wait_for_text_visible(surface: str, marker: str, timeout_s: int = SUBMIT_VERIFY_TIMEOUT_S, poll_s: float = 0.5) -> bool:
    """Poll until `marker` appears in the pane (the typed prompt landed in the input).

    Best-effort: returns True on detect, False on timeout. Callers proceed
    regardless because the transcript-retry loop is the backstop."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if marker in read_surface(surface):
            return True
        time.sleep(poll_s)
    return False


# ---------------------------------------------------------------------------
# Task 5: Agent tab (launch claude, submit prompt)
# ---------------------------------------------------------------------------

def _claude_session_dir(worktree: str) -> Path:
    return Path.home() / ".claude" / "projects" / ("-" + str(worktree).strip("/").replace("/", "-"))


def _claude_transcripts(worktree: str) -> set:
    d = _claude_session_dir(worktree)
    return set(d.glob("*.jsonl")) if d.is_dir() else set()


def open_agent_tab(workspace: str, prompt: str, worktree: str) -> str:
    """Launch claude in a new terminal surface.

    If `prompt` is falsy (None or ""), starts claude idle (no prompt sent) -- used when
    re-spinning an existing branch where writing-plans has already run.

    If `prompt` is provided, submits it: waits for Claude's `❯` input box before sending,
    then waits for the prompt prefix to appear before submitting. The transcript-retry
    backstop re-sends Enter if Claude did not actually start a conversation."""
    surface = cmux_new_surface(workspace, surface_type="terminal")
    cmux_send(surface, "claude")                   # resilient: wakes the backend + retries
    cmux_send_key(surface, "enter")
    wait_for_claude_ready(surface)                 # replaces: time.sleep(CLAUDE_READY_DELAY_S)
    if not prompt:
        return surface
    before = _claude_transcripts(worktree)
    cmux_send(surface, prompt)
    wait_for_text_visible(surface, prompt[:24])    # replaces: time.sleep(SUBMIT_DELAY_S)
    cmux_send_key(surface, "enter")
    # Backstop: confirm the conversation actually started; re-send Enter if not.
    deadline = time.monotonic() + SUBMIT_VERIFY_TIMEOUT_S
    while time.monotonic() < deadline:
        if _claude_transcripts(worktree) - before:
            break
        time.sleep(2.0)
        cmux_send_key(surface, "enter")
    return surface


# ---------------------------------------------------------------------------
# Task 6: Browser tab
# ---------------------------------------------------------------------------

def open_browser_tab(workspace: str, name: str) -> str:
    return cmux_new_surface(workspace, surface_type="browser", url=test_url_for(name))


def open_ref_browser_pane(workspace: str, ref_url: str) -> None:
    """Open ref_url (Jira ticket or GitHub PR) in its own browser pane, separate from
    the test-app browser. Best-effort: a failure must not break the chain."""
    try:
        _cmux(["new-pane", "--workspace", workspace, "--type", "browser", "--url", ref_url])
    except CmuxError:
        pass


# ---------------------------------------------------------------------------
# Task 7: Orchestrate the chain (gating + notify)
# ---------------------------------------------------------------------------

def run_chain(name: str, branch: str, new_branch: bool, prompt: str, ref_url: str = None,
              group: dict = None) -> dict:
    """Create the worktree and run the four-tab cmux chain with gating.
    Returns {"status": ok|setup-failed|serving-timeout|port-reassign-failed,
             "workspace": ref, "worktree": path}.
    If ref_url is set, opens a separate reference browser pane (Jira ticket or GitHub PR)
    unconditionally after the test-app browser logic.
    If group is set (a GROUP_IN_PROGRESS or GROUP_PR_REVIEWS dict), the workspace is
    assigned to that group after creation (best-effort; failure never aborts the chain)."""
    worktree = ensure_worktree(name, branch, new_branch)
    workspace = open_setup_tab(worktree, name)

    if not wait_for_setup(worktree):
        cmux_notify(f"{name}: setup failed", "see the setup tab", workspace=workspace)
        return {"status": "setup-failed", "workspace": workspace, "worktree": worktree}

    # record workspace -> worktree so the close-event listener can resolve closes
    try:
        record_workspace_map(workspace, name)
    except Exception as e:
        print(f"warning: could not record workspace map ({e})", file=sys.stderr)

    if group:
        assign_to_group(workspace, group)

    port = read_worktree_port(worktree)
    if port is not None and port_in_use(port):
        # If it's OUR own orphaned puma (no live overmind), reclaim the port.
        if not dev_server_up(worktree):
            kill_worktree_port_orphans(worktree, port)
            time.sleep(1)
        # If still in use, it belongs to another tool -> reassign to a free port.
        if port_in_use(port):
            free = find_free_port(port + 1)
            if rerun_setup_with_port(worktree, name, free):
                cmux_notify(f"{name}: moved to port {free}",
                            f"port {port} was in use; reconfigured to {free}",
                            workspace=workspace)
                port = free
            else:
                cmux_notify(f"{name}: port reassign failed",
                            f"port {port} in use and re-setup on {free} failed",
                            workspace=workspace)
                return {"status": "port-reassign-failed", "workspace": workspace,
                        "worktree": worktree, "port": port}

    cmux_focus_workspace(workspace)  # re-assert foreground after the setup wait
    open_dev_server_tab(workspace, worktree)
    serving = wait_for_dev_server(worktree)

    cmux_focus_workspace(workspace)  # re-assert again after the dev-server wait
    open_agent_tab(workspace, prompt, worktree)
    if serving:
        open_browser_tab(workspace, name)
        cmux_notify(f"{name} ready", "plan running", workspace=workspace)
        status = "ok"
    else:
        cmux_notify(f"{name}: dev server slow", "agent started; open browser manually",
                    workspace=workspace)
        status = "serving-timeout"

    if ref_url:
        open_ref_browser_pane(workspace, ref_url)

    return {"status": status, "workspace": workspace, "worktree": worktree}


# ---------------------------------------------------------------------------
# Phase 2: PR notify-state helpers + cmux-poll + seed-backlog subcommands
# ---------------------------------------------------------------------------

def load_pr_notify_state() -> dict:
    """Read PR notification state. Missing or corrupted -> default empty state."""
    try:
        with open(PR_NOTIFY_STATE_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict) and "numbers" in data and "notif_id" in data:
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"numbers": [], "notif_id": None}


def save_pr_notify_state(state: dict) -> None:
    """Persist PR notification state to cache file. Creates parent dir if needed."""
    PR_NOTIFY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PR_NOTIFY_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def cmux_list_notifications() -> list:
    """Return a list of dicts {id, workspace, surface, read, title, body, scope}
    parsed from `cmux list-notifications` pipe-delimited output.

    Line format (index 0 is most recent):
      <index>:<NOTIF_UUID>|<workspace_id>|<surface_id>|<read|unread>|<title>||<body>|<created_at>|<scope>
    """
    try:
        out = _cmux(["list-notifications"])
    except CmuxError:
        return []
    results = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip the leading "<index>:" prefix, then split on |
        colon_idx = line.find(":")
        if colon_idx == -1:
            continue
        rest = line[colon_idx + 1:]
        parts = rest.split("|")
        # Expected: uuid, workspace, surface, read_status, title, (empty), body, created_at, scope
        if len(parts) < 5:
            continue
        notif_id = parts[0]
        read_status = parts[3] if len(parts) > 3 else ""
        title = parts[4] if len(parts) > 4 else ""
        body = parts[6] if len(parts) > 6 else ""
        scope = parts[8] if len(parts) > 8 else ""
        results.append({
            "id": notif_id,
            "read": read_status == "read",
            "title": title,
            "body": body,
            "scope": scope,
        })
    return results


def cmux_dismiss_notification(notif_id: str) -> None:
    """Best-effort dismiss a single notification by UUID. Swallows errors."""
    try:
        _cmux(["dismiss-notification", "--id", notif_id])
    except CmuxError:
        pass


def find_notification_id_by_title(title: str) -> "str | None":
    """Return the UUID of the most-recent notification whose title matches exactly.
    Returns None if not found or if listing fails."""
    for notif in cmux_list_notifications():
        if notif["title"] == title:
            return notif["id"]
    return None


def main_workspace_ref() -> "str | None":
    """Look up the workspace named 'Main' from `cmux workspace list`.
    Returns its ref/id if found, None otherwise. Never raises."""
    try:
        out = _cmux(["workspace", "list"])
        for line in out.splitlines():
            # Lines typically: "workspace:N  Name  ..."
            m = _WS_RE.search(line)
            if m and "Main" in line:
                return m.group(0)
    except CmuxError:
        pass
    return None


def within_work_hours() -> bool:
    """True iff the current local time is a weekday (Mon-Fri) and hour is 8-16 inclusive."""
    now = datetime.datetime.now()
    return 1 <= now.isoweekday() <= 5 and 8 <= now.hour <= 16


def cmd_cmux_poll(args) -> int:
    jira_spun = []
    if within_work_hours():
        try:
            eligible = sh.list_assigned_eligible()
        except sh.JiraError as e:
            print(f"jira-error: {e}", file=sys.stderr); eligible = []
        already = {k for k, v in sh.load_state().items() if v.get("spunup_at") is not None}
        for t in eligible:
            if t["key"] in already:
                continue
            branch = sh.derive_branch(t["key"], t["issue_type"], t["title"])
            name = worktree_name_for_ticket(t["key"])
            try:
                sh.transition_to_in_progress(t["key"], t["status"])
                result = run_chain(name=name, branch=branch, new_branch=True, prompt=jira_agent_prompt(t["key"], branch),
                                   ref_url=f"{JIRA_BROWSE_BASE}/{t['key']}", group=GROUP_IN_PROGRESS)
            except (sh.JiraError, CmuxError, GitError) as e:
                print(f"cmux-spinup-failed {t['key']}: {e}", file=sys.stderr)
                break
            if result["status"] not in ("ok", "serving-timeout"):
                print(f"cmux-spinup-failed {t['key']}: run_chain status={result['status']}", file=sys.stderr)
                break
            sh.mark_spunup(t["key"])
            jira_spun.append({"key": t["key"], "name": name})
            break

        # Jira-only notification (PRs handled separately below)
        if jira_spun:
            j = len(jira_spun)
            title = f"cmux: {j} spun up"
            lines = [f"✓ {x['key']} ready (cmux workspace {x['name']})" for x in jira_spun]
            cmux_notify(title, "; ".join(lines))

    # --- PR notification: maintain one always-current "pending PR reviews" notification ---
    try:
        prs = sh.list_review_requests()
    except sh.GitHubError as e:
        print(f"gh-error: {e}", file=sys.stderr); prs = []

    # Record any newly-seen PRs (idempotent)
    for pr in prs:
        sh.mark_pr_surfaced(pr["number"], pr)

    # Compute pending: open PRs the user has NOT yet acted on (decision not review or skip)
    pr_state = sh.load_pr_state()
    open_numbers = {pr["number"] for pr in prs}
    pending = [
        pr for pr in prs
        if pr["number"] in open_numbers
        and pr_state.get(str(pr["number"]), {}).get("decision", "pending")
        not in ("review", "skip")
    ]
    current = sorted(pr["number"] for pr in pending)

    notify_state = load_pr_notify_state()
    if current != notify_state["numbers"]:
        # Dismiss the old notification if one exists
        if notify_state["notif_id"]:
            cmux_dismiss_notification(notify_state["notif_id"])

        if current:
            n = len(current)
            pr_title = f"cmux: {n} PR review pending" if n == 1 else f"cmux: {n} PR reviews pending"
            # Build a single-line body sorted by number. cmux notify truncates the
            # body at the first newline, so everything must live on one line.
            pending_by_num = {pr["number"]: pr for pr in pending}
            listed = ", ".join(
                f"#{num} {pending_by_num[num]['author']}" for num in current
            )
            body = f"{listed} -- /spinup #N"
            ws_ref = main_workspace_ref()
            cmux_notify(pr_title, body, workspace=ws_ref)
            notif_id = find_notification_id_by_title(pr_title)
            save_pr_notify_state({"numbers": current, "notif_id": notif_id})
        else:
            save_pr_notify_state({"numbers": [], "notif_id": None})
    # else: pending set unchanged -> leave existing notification in place, post nothing

    # Sweep the Archive group for finished worktrees. Do NOT auto-create the group here:
    # workspace.group.create grabs the currently-selected workspace, and the sweep would
    # then tear that workspace down on the same cycle (this is what destroyed proj-5864).
    # The Archive group is created once out-of-band; if absent, sweep is a safe no-op.
    try:
        sweep_archive_group()
    except Exception as e:
        print(f"archive-sweep error: {e}", file=sys.stderr)

    print(json_dumps_result("cmux-poll", "", {"jira_spun_up": jira_spun, "prs_to_surface": [
        pr for pr in prs if pr["number"] in {p["number"] for p in pending}
    ]}))
    return 0


def cmd_seed_backlog(args) -> int:
    elig = sh.list_assigned_eligible()
    for t in elig:
        sh.mark_spunup(t["key"])
    prs = sh.list_review_requests()
    for pr in prs:
        sh.mark_pr_surfaced(pr["number"], pr)
    print(json_dumps_result("seed-backlog", "",
          {"seeded_tickets": [t["key"] for t in elig], "seeded_prs": [p["number"] for p in prs]}))
    return 0


# ---------------------------------------------------------------------------
# Task 8: cmux-spinup / cmux-spinup-pr subcommands + main()
# ---------------------------------------------------------------------------

def jira_agent_prompt(key: str, branch: str) -> "str | None":
    """Return the writing-plans prompt for a Jira ticket, or None (idle agent) ONLY when the
    branch already has real in-progress work (exists AND has commits ahead of main).

    A branch that merely exists at base HEAD with no commits -- e.g. left behind by a prior or
    failed spin-up -- still gets writing-plans. Idle is reserved for genuine re-spins of work
    where a plan has already been written; otherwise a recovered ticket would silently skip
    planning (which it did for proj-5761/proj-5884)."""
    if branch_exists(branch) and branch_has_commits(branch):
        return None
    return sh.jira_work_prompt(key)


def git_fetch(branch: str) -> None:
    """Fetch a remote branch into the main checkout so a worktree can attach it."""
    subprocess.run(["git", "-C", str(MAIN_REPO), "fetch", "origin", branch],
                   capture_output=True, text=True)


def cmd_spinup(args) -> int:
    try:
        ticket = sh.fetch_ticket(args.ticket)
    except sh.JiraError as e:
        print(f"could not fetch {args.ticket}: {e}", file=sys.stderr); return 2
    branch = sh.derive_branch(ticket["key"], ticket["issue_type"], ticket["title"])
    name = worktree_name_for_ticket(ticket["key"])
    try:
        sh.transition_to_in_progress(ticket["key"], ticket["status"])
    except sh.JiraError as e:
        print(f"transition failed: {e}", file=sys.stderr); return 3
    result = run_chain(name=name, branch=branch, new_branch=True,
                       prompt=jira_agent_prompt(ticket["key"], branch),
                       ref_url=f"{JIRA_BROWSE_BASE}/{ticket['key']}",
                       group=GROUP_IN_PROGRESS)
    if result["status"] in ("ok", "serving-timeout"):
        sh.mark_spunup(ticket["key"])
    print(json_dumps_result(ticket["key"], branch, result))
    return 0


def cmd_spinup_pr(args) -> int:
    number = int(args.number)
    try:
        branch = sh.resolve_pr_head_branch(number)
    except sh.GitHubError as e:
        print(f"could not resolve PR #{number}: {e}", file=sys.stderr); return 2
    git_fetch(branch)
    result = run_chain(name=worktree_name_for_pr(number), branch=branch, new_branch=False,
                       prompt=sh.pr_work_prompt(number),
                       ref_url=f"https://github.com/{GH_REPO}/pull/{number}",
                       group=GROUP_PR_REVIEWS)
    print(json_dumps_result(f"PR-{number}", branch, result))
    return 0


def json_dumps_result(ref: str, branch: str, result: dict) -> str:
    return json.dumps({"ref": ref, "branch": branch, **result}, indent=2)


def main():
    p = argparse.ArgumentParser(prog="cmux_chain")
    sub = p.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("spinup"); s1.add_argument("ticket")
    s2 = sub.add_parser("spinup-pr"); s2.add_argument("number", type=int)
    sub.add_parser("cmux-poll")
    sub.add_parser("seed-backlog")
    p_spindown = sub.add_parser("spindown", help="Tear down a worktree (or the focused one)")
    p_spindown.add_argument("name", nargs="?", default=None)
    p_spindown.set_defaults(func=cmd_spindown)
    p_listen = sub.add_parser("close-listen", help="Stream workspace.closed and tear down worktrees")
    p_listen.set_defaults(func=cmd_close_listen)
    args = p.parse_args()
    sys.exit({
        "spinup": cmd_spinup,
        "spinup-pr": cmd_spinup_pr,
        "cmux-poll": cmd_cmux_poll,
        "seed-backlog": cmd_seed_backlog,
        "spindown": cmd_spindown,
        "close-listen": cmd_close_listen,
    }[args.cmd](args))


if __name__ == "__main__":
    main()
