# Design Spec: Paseo + Pi Remote Session Sharing via Tmux Shim

**Date:** 2026-08-18
**Status:** Approved
**Related:** Earendil Pi 0.84.1, Paseo 0.1.78, cmux surface layout

## Overview

Pi sessions today exist only in the local terminal where they are launched. Paseo
can open a managed terminal window alongside any cmux surface, but it has no
mechanism to attach to an already-running Pi TUI. The goal is to let a single Pi
session be visible in BOTH the local pane (cmux or ordinary terminal) AND a
Paseo-managed terminal at the same time, while keeping all session state in one
process.

The selected approach is a tmux-session-sharing shim: a global wrapper installed
at `~/.pi/agent/bin/pi` intercepts interactive invocations, starts the real Pi
binary inside a uniquely named tmux session, then uses the Paseo CLI to
open a terminal that attaches to that session. Both panes show the same TUI
rendered by tmux. Cleanup is self-contained: the Paseo terminal's startup command
runs the attach and then kills itself when the tmux attach returns.

## Goals

- Interactive `pi` invocations remain the single entry point. No new subcommands
  or mental model changes for the user.
- The Pi TUI is visible in both the local pane and the Paseo pane via tmux session
  sharing. Both panes write to the same pty.
- Session state lives in exactly one process. Both views are clients of the same
  tmux session.
- Paseo's terminal closes automatically when Pi exits. No polling loop, no helper
  script, and no state files are required.
- Noninteractive and utility invocations are completely unaffected.
- The shim is safe to leave in place permanently; disabling is a single env var.
- If Paseo setup fails, Pi still launches locally with a clear error on stderr.
  The session is never implied to be remotely available when setup has failed.

## Non-Goals

- Multiplexing Pi across more than one Paseo terminal at a time. The primary use
  case is the local pane plus one Paseo pane.
- Replacing the Pi TUI with a web-based or Paseo-native rendering surface.
- Using Paseo's native Pi provider (see Approach A below and the version mismatch
  section).
- Supporting Pi invocations that are already inside a tmux session in a way that
  creates nested sessions (recursion is explicitly avoided; see below).
- Synchronizing Pi session history or conversation context across machines.
- Modifying the Pi binary itself or the Paseo binary.

## Background: Why Not Paseo's Native Pi Provider?

Paseo 0.1.78 ships with a built-in Pi provider that can launch Pi from within a
Paseo surface. However, Paseo embeds its own upstream Pi at version 0.70.2. The
installed Earendil build is Pi 0.84.1, which includes significant capability
improvements, the current skills layout, and agentfiles-managed customization
that 0.70.2 does not know about. Selecting Paseo's native provider would
silently downgrade the Pi version and bypass the shim chain. For this reason the
native provider is explicitly not selected, and the design instead delegates from
the shim to the installed binary.

## Considered Approaches

### Approach A: Paseo Native Pi Provider (Rejected)

Use Paseo's built-in Pi provider to launch sessions from within the Paseo UI.

**Why rejected:** Embeds Pi 0.70.2. Bypasses the installed Earendil Pi 0.84.1
and all agentfiles customization (shim, skills, hooks). Version skew is silently
invisible to the user.

### Approach B: PTY Relay / Proxy Process (Rejected)

Run a relay process that spawns Pi, captures its pty output, and forwards it to
two consumers (the local terminal and a Paseo terminal socket).

**Why rejected:** Writing a reliable pty multiplexer is complex (flow control,
terminal resize events, color escape passthrough, inline image protocols). Any
relay process becomes a single point of failure that swallows output if it
crashes. Tmux already solves this problem correctly.

### Approach C: Tmux Session Sharing Shim (Selected)

A thin shell wrapper at `~/.pi/agent/bin/pi` creates a named tmux session,
launches the real Pi inside it, uses `paseo terminal create` to open a managed
terminal window, and sends that window a self-cleaning attach command. The local
terminal then attaches to the same tmux session.

**Why selected:** Tmux handles all pty complexity. Session sharing is its primary
design goal. Paseo integration uses the `paseo terminal` CLI (create, send-keys,
kill) with no daemon socket contract to maintain. Cleanup is event-driven and
self-contained in the Paseo terminal's startup command chain: when the tmux attach
returns, the terminal immediately kills itself. No helper scripts, no state files,
and no polling loops are required.

## Prerequisites

### Daemon and CLI Requirements

The following must be available for the full integration. The shim checks each
before proceeding; missing dependencies trigger the fail-clearly behavior
described below.

- **tmux** -- must be installed and available in PATH.
- **Paseo daemon** -- probed with `paseo status --json`. If the daemon is not
  running, started with `paseo start` before proceeding. If `paseo start` fails or
  `paseo status --json` still reports not-ready afterward, the shim falls back to
  direct Pi and reports the failure clearly.
- **Paseo CLI** -- `paseo terminal create`, `paseo terminal send-keys`, and
  `paseo terminal kill` must be available in PATH.
- **Real Pi binary** -- located via `PI_REAL` (env) or the path recorded in
  `~/.pi/agent/pi-real` at bootstrap install time, skipping the shim itself to
  avoid recursion (see Recursion Avoidance below).
- **`jq`** -- used to extract the terminal `id` from `paseo terminal create
  --json` output. If absent, the shim falls back to direct Pi.

### Installed Layout

```
~/.pi/agent/bin/pi           # the shim; must appear BEFORE real pi in PATH
~/.pi/agent/pi-real          # path to the real Pi binary (written at install time)
```

The shim is installed by the agentfiles bootstrap as part of Pi tool setup.

## Design

### Entry Point: The Shim

`~/.pi/agent/bin/pi` is a POSIX shell script. It is placed first in `PATH` via
the agentfiles shell integration so that typing `pi` or invoking `pi` from any
tool or hook always hits the shim.

```
~/.pi/agent/bin/
  pi            <-- shim (this script)
```

The shim executes the following decision tree on every invocation:

```
pi <args>
  |
  +-- PI_NO_PASEO=1 set?  --> exec real Pi directly, no tmux, no Paseo
  |
  +-- Already inside a tmux session? (TMUX env var set)
  |     --> exec real Pi directly, no wrapping (recursion avoidance)
  |
  +-- Noninteractive invocation? (see below)
  |     --> exec real Pi directly, no tmux, no Paseo
  |
  +-- tmux not found?
  |     --> warn to stderr, exec real Pi directly in local terminal
  |
  +-- Paseo CLI not in PATH?
  |     --> warn to stderr, exec real Pi directly in local terminal
  |
  +-- Paseo daemon not running? (paseo status --json)
  |     +-- paseo start succeeds?
  |     |     --> continue
  |     +-- paseo start fails?
  |           --> report failure clearly to stderr, exec real Pi directly
  |               (Pi launches locally; session is not remotely available)
  |
  +-- Proceed with full Paseo integration:
        1. Generate unique tmux session name
        2. Create tmux session with real Pi as the only pane command
        3. Create Paseo terminal and send self-cleaning attach command
        4. Attach local terminal to tmux session
        5. Block until tmux session exits
```

### Noninteractive Bypass Conditions

The following invocations exec the real Pi binary directly without tmux or Paseo.

The primary gate is a tty check: if stdout or stdin is not a tty, the invocation
is noninteractive and bypasses the shim entirely:

```sh
[ -t 0 ] && [ -t 1 ] || exec "$PI_REAL" "$@"
```

Additionally, the shim scans arguments and bypasses when any of the following
are present, because these invocations are logically noninteractive even in a tty:

- `--help` or `-h`
- `--version` or `-v`
- `--print` or `-p`
- `--mode json`
- `--mode rpc`
- `--export`
- `--list-models`
- A leading subcommand that is one of: `install`, `remove`, `uninstall`,
  `update`, `list`, `config`, `auth`

### Session Naming

Each invocation that proceeds through the Paseo path creates a new uniquely named
tmux session. The name format is:

```
pi-<timestamp>-<random4>
```

Where `<timestamp>` is seconds since epoch and `<random4>` is four hex digits from
`/dev/urandom`. Example: `pi-1755523812-a3f1`.

### Paseo Terminal Creation and Self-Cleaning Attach

After the tmux session exists and the Pi pane is running, the shim creates a
Paseo-managed terminal window and sends it a command that attaches to the tmux
session and then kills itself when the attach returns:

```sh
TERM_JSON=$(paseo terminal create \
  --cwd "$PWD" \
  --name "pi ($SESSION)" \
  --json)
TERM_ID=$(printf '%s' "$TERM_JSON" | jq -r '.id')

# Build a safely quoted self-cleaning attach command and send it to the terminal.
ATTACH_CMD="tmux attach-session -t '${SESSION}'; paseo terminal kill '${TERM_ID}' --json"
paseo terminal send-keys "$TERM_ID" "$ATTACH_CMD" Enter
```

`paseo terminal create --json` returns `{"id":"...","name":"...","cwd":"..."}`.
The shim extracts `id` to use in the kill command.

When the Paseo terminal's shell executes this command:

1. `tmux attach-session -t <session>` blocks while Pi is running. The Paseo pane
   shows the same TUI as the local pane.
2. When Pi exits and tmux returns, `;` runs `paseo terminal kill <id> --json`.
3. The Paseo terminal closes.

No helper scripts, no state files, no tmux hooks, and no polling are involved.

On error from `paseo terminal create`, the shim prints a clear warning to stderr
and continues: the local tmux attach proceeds and Pi runs locally. No remote pane
is opened in this case; the user is not misled into thinking a remote session is
available.

If `paseo terminal send-keys` fails after `paseo terminal create` succeeded, the
shim immediately kills the orphaned terminal (`paseo terminal kill "$TERM_ID"
--json`) to avoid leaving a dangling Paseo window, then reports to stderr that
remote access failed, then continues with the local tmux attach so Pi still
launches. The shim never implies remote access is available after a send-keys
failure.

### Argument Forwarding

All arguments passed to the shim are forwarded verbatim to the real Pi binary
inside tmux:

```sh
tmux new-session -d -s "$SESSION" \
  "$PI_REAL" "$@"
```

The shim does not inspect, filter, or modify any arguments beyond the bypass
checks listed above. Environment variables are inherited unchanged from the
calling process. `PI_NO_PASEO` is unset before the `tmux new-session` call so
the bypass flag does not propagate into Pi or any Pi-spawned subprocesses.
`PI_IN_SHIM` is left set so the recursion guard remains active for any nested
`pi` invocations that explicitly unset `TMUX`.

### Recursion Avoidance

The `TMUX` environment variable is set by tmux in all processes running inside a
tmux pane. The shim checks for `TMUX` at the top of its decision tree. If `TMUX`
is set, the shim execs the real Pi binary directly without creating a new session.
This prevents the shim from wrapping itself when Pi is invoked from a hook, a
subagent, or any other context that is already inside a tmux pane.

`PI_IN_SHIM=1` is also exported before launching tmux, so that any `pi`
invocation from within the Pi process itself (e.g. from a hook or subagent that
explicitly unsets `TMUX`) still hits the recursion guard and execs the real
binary directly.

### Emergency Bypass

Setting `PI_NO_PASEO=1` in the environment causes the shim to exec the real Pi
binary directly:

```sh
PI_NO_PASEO=1 pi
```

This bypass is silent (no warning printed). It is intended for:

- Debugging the shim itself without triggering Paseo.
- Running Pi in environments where Paseo is intentionally absent (CI, remote SSH
  without the Paseo daemon, Docker containers).
- Recovering from a broken shim without modifying PATH.

`PI_REAL` can also be set to an explicit path to override the resolved real Pi
binary location. This is useful when testing a new Pi build alongside the
production installation.

### Fail-Clearly Behavior

The shim must never silently degrade. Each failure condition has a defined
observable output on stderr, and Pi always launches locally when the shim falls
back:

| Condition | Behavior |
| ----------- | ---------- |
| tmux not installed | `pi-shim: tmux not found; launching pi directly` then exec real Pi |
| Paseo CLI not in PATH | `pi-shim: paseo not found in PATH; launching pi directly` then exec real Pi |
| jq not in PATH | `pi-shim: jq not found in PATH; launching pi directly` then exec real Pi |
| paseo start fails | `pi-shim: paseo daemon failed to start; launching pi directly` then exec real Pi |
| tmux new-session fails | `pi-shim: failed to create tmux session; launching pi directly` then exec real Pi |
| paseo terminal create fails | `pi-shim: paseo terminal create failed; Pi running locally only` then local tmux attach proceeds |
| paseo terminal send-keys fails | `pi-shim: paseo terminal send-keys failed; remote access unavailable` then kill the orphaned terminal (`paseo terminal kill <id> --json`), then local tmux attach proceeds |
| Real Pi binary not found | `pi-shim: real pi binary not found; check PI_REAL or ~/.pi/agent/pi-real` exit 1 |

In every fallback case where Pi can be launched, it is. The Paseo path is
best-effort for all steps after the tmux session is created. Fallback messages go
to stderr; scripted callers can suppress them with `2>/dev/null`.

## Lifecycle Summary

```
User types: pi [args]
  |
  v
~/.pi/agent/bin/pi (shim)
  |-- bypass checks (TMUX, PI_NO_PASEO, noninteractive flags, non-tty)
  |-- prerequisite checks (tmux present, paseo in PATH, jq in PATH,
  |                        paseo status --json / paseo start, PI_REAL)
  |
  v (full path)
tmux new-session -d -s pi-<ts>-<rand> "$PI_REAL" [args]
  |                                        |
  |                                        v
  |                                   Pi TUI running inside tmux session
  |
  v
paseo terminal create --cwd "$PWD" --name "pi (pi-<ts>-<rand>)" --json
  --> returns { "id": "<term-id>", "name": "...", "cwd": "..." }
  |
  v
paseo terminal send-keys <term-id> \
  "tmux attach-session -t 'pi-<ts>-<rand>'; paseo terminal kill '<term-id>' --json" \
  Enter
  |
  v
Paseo terminal executes: tmux attach-session -t pi-<ts>-<rand>
  --> Paseo pane now shows the same TUI
  |
  v
shim: tmux attach-session -t pi-<ts>-<rand>   (local terminal joins)
  |
  v
[User interacts via either pane]
  |
  v
Pi exits; tmux session ends
  |
  +-- Local terminal: tmux returns, shim exits, shell prompt restored
  |
  +-- Paseo terminal: tmux attach returns, shell runs next command:
        paseo terminal kill <term-id> --json
        --> Paseo terminal window closes
```

## Caveats

### Tmux Resizing

When both the local pane and the Paseo pane are attached to the same tmux session,
tmux sizes the session to the smaller of the two terminal dimensions. This is
standard tmux multi-client behavior. If the Paseo terminal is narrower than the
local pane, the Pi TUI will appear constrained in the local view. Subsequent
resizes from either terminal update the session size dynamically.

The shim passes the calling terminal's current dimensions as the initial session
size (`-x "$COLUMNS" -y "$LINES"`) when creating the tmux session. If those
variables are unset, tmux uses its default width and height.

### Inline Image Support

Pi may use terminal graphics protocols (Kitty graphics, Sixel) for inline images.
Tmux does not pass these escape sequences through to attached clients unchanged.
Inline images may not render correctly in either the local pane or the Paseo pane
when running through the shim.

Users who require inline image rendering should use `PI_NO_PASEO=1` to bypass the
shim for those sessions.

### Security Implications

- **Session name predictability:** The session name `pi-<timestamp>-<rand4>` is
  not a secret. Any local user with access to `tmux list-sessions` can see active
  Pi sessions and their names. The random suffix is for uniqueness, not secrecy.
  On multi-user machines, use a per-user tmux server socket.
- **Argument forwarding:** The shim forwards all arguments verbatim to Pi. No
  injection surface is introduced that was not already present in direct Pi
  invocation.
- **Shim permissions:** The shim must not be writable by any user other than the
  owner (`chmod 0755`, world-write forbidden).
- **Remote access is privileged:** Any Paseo terminal attached to the tmux
  session has effective local account shell access, not just Pi access. The tmux
  session can be used to create or switch windows, send keystrokes to arbitrary
  panes, or otherwise escape the Pi TUI. Any Paseo relay, pairing mechanism, or
  external controller that can reach this terminal must be treated as having
  local account privileges. Do not grant Paseo remote access to untrusted parties.

## Validation Expectations

The following checks constitute a passing validation of the integration:

1. **Shim is hit:** `which pi` returns `~/.pi/agent/bin/pi`. The real binary is
   reachable at the path recorded in `~/.pi/agent/pi-real`.
2. **Version forwarded correctly:** `pi --version` (noninteractive bypass) prints
   the Earendil 0.84.1 version string, not 0.70.2.
3. **Noninteractive bypass confirmed:** `pi --version`, `pi --list-models`, and
   `echo '' | pi` do not create any tmux sessions. `tmux list-sessions` is
   unchanged before and after each call.
4. **Interactive session creates tmux session:** Running `pi` in an interactive
   terminal creates exactly one new tmux session matching `pi-*`. `tmux
   list-sessions` shows it.
5. **Paseo pane appears:** `paseo terminal create` succeeds and the terminal opens
   showing the Pi TUI after the send-keys command runs.
6. **Both panes connected:** Typing in either pane is reflected in both views
   because both are clients of the same tmux session.
7. **Cleanup on exit:** Closing Pi causes the Paseo terminal to close
   automatically (via the self-cleaning command chain). `tmux list-sessions` no
   longer shows the session afterward.
8. **PI_NO_PASEO bypass:** `PI_NO_PASEO=1 pi` launches Pi in the local terminal
   with no tmux session created and no Paseo terminal opened.
9. **Paseo-absent fallback:** With `paseo start` unable to reach a daemon, running
   `pi` prints a clear failure message to stderr and launches Pi locally with no
   tmux wrapping and no implication of remote availability.
10. **Recursion guard:** Running `pi` from inside a tmux pane (with `TMUX` set)
    launches Pi directly without creating a nested tmux session.
