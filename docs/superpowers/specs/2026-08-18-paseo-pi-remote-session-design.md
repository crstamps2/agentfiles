# Design Spec: Paseo + Pi Remote Session Sharing via Tmux Shim

**Date:** 2026-08-18
**Status:** Approved
**Related:** Earendil Pi 0.84.1, Paseo 0.1.78, cmux surface layout

## Overview

Pi sessions today exist only in the local terminal where they are launched. Paseo
can open a managed terminal window alongside any cmux surface, but it has no
mechanism to attach to an already-running Pi TUI. The goal is to let a single Pi
session be visible in BOTH the local pane (cmux or ordinary terminal) AND a
Paseo-managed terminal at the same time, with full interactive TUI fidelity in
both views, while keeping all session state in one place.

The selected approach is a tmux-session-sharing shim: a global wrapper installed
at `~/.pi/agent/bin/pi` intercepts interactive invocations, starts the real Pi
binary inside a deterministically named tmux session, then signals Paseo to open
a terminal that attaches to the same session. Both panes show the identical TUI.

## Goals

- Interactive `pi` invocations remain the single entry point. No new subcommands
  or mental model changes for the user.
- The full Pi TUI is preserved. Paseo's view is a real tmux attach, not a
  transcript or screenshot stream.
- Session state lives in exactly one process. Both views write to the same pty.
- Paseo's terminal closes automatically when Pi exits. No polling loop required.
- Noninteractive and utility invocations are completely unaffected.
- The shim is safe to leave in place permanently; disabling is a single env var.
- Failure in Paseo setup does not silently swallow Pi. Pi still launches locally.

## Non-Goals

- Multiplexing Pi across more than two terminal windows simultaneously. The design
  handles one local pane + one Paseo pane; additional viewers are out of scope.
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
relay process becomes a single point of failure that swallows output if it crashes.
Tmux already solves this problem correctly.

### Approach C: Tmux Session Sharing Shim (Selected)

A thin shell wrapper at `~/.pi/agent/bin/pi` creates a named tmux session,
launches the real Pi inside it, signals Paseo via its local relay to open an
attaching terminal, and blocks waiting for tmux to exit. Cleanup is event-driven
via a tmux `remain-on-exit off` and an `after-hook` on the Pi pane exit.

**Why selected:** Tmux handles all pty complexity. Session sharing is its primary
design goal. The shim itself is under 60 lines of POSIX shell. Paseo integration
is a single HTTP or socket call to an already-running Paseo relay. The design
composes independently: both halves (tmux launch and Paseo notification) are
tested and used independently of each other.

## Prerequisites

### Daemon / Relay Requirements

The following services must be running for the full integration to work. The shim
checks each before proceeding with the Paseo path; missing services trigger the
fail-clearly behavior described below.

- **tmux >= 3.2** -- required for `remain-on-exit` behavior and reliable
  `send-keys`/`new-session` flags. Version is checked at shim entry.
- **Paseo relay** -- a local HTTP or Unix-socket relay (`paseo relay`) must be
  running and accepting requests on a well-known address (default:
  `$PASEO_RELAY_SOCK` or `~/.pi/agent/paseo.sock`). The shim tests reachability
  with a short-timeout probe before attempting session setup.
- **Real Pi binary** -- the shim locates the real Pi binary via `PI_REAL` (env)
  or by reading the path recorded in `~/.pi/agent/pi-real` at install time,
  skipping itself to avoid recursion (see Recursion Avoidance below).

### Installed Layout

```
~/.pi/agent/bin/pi           # the shim (this script); must appear BEFORE real pi in PATH
~/.pi/agent/paseo.sock       # default Paseo relay socket path (overridable via env)
~/.pi/agent/sessions/        # per-session state dir (session name, pid file)
```

The shim is installed by the agentfiles bootstrap as part of Pi tool setup. The
real Pi binary location is resolved at install time and written to
`~/.pi/agent/pi-real` so the shim does not have to re-search PATH every
invocation.

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
  +-- tmux not found or wrong version?
  |     --> warn to stderr, exec real Pi directly in local terminal
  |
  +-- Paseo relay unreachable?
  |     --> warn to stderr, exec real Pi directly in local terminal
  |         (Pi must still launch; Paseo is best-effort)
  |
  +-- Proceed with full Paseo integration:
        1. Generate unique tmux session name
        2. Create tmux session with real Pi as the only pane command
        3. Signal Paseo relay to open attaching terminal
        4. Attach local terminal to tmux session
        5. Register exit hook for Paseo cleanup
        6. Block until tmux session exits
```

### Noninteractive Bypass Conditions

The following invocations exec the real Pi binary directly without tmux or Paseo:

- `pi --help` or `pi -h` (any argument that is exactly `--help` or `-h`)
- `pi --version` or `pi -v`
- `pi models` or `pi model-list` (or any subcommand that produces machine-readable output)
- `pi --json` flag present anywhere in the argument list
- `pi --print` flag present anywhere in the argument list
- Any invocation where stdout is not a tty (`[ -t 1 ]` is false) -- pipes, file
  redirects, and CI-style script invocations are noninteractive by definition
- Any invocation where stdin is not a tty (`[ -t 0 ]` is false) -- RPC-style
  usage driving Pi with piped input

The check `[ -t 1 ] && [ -t 0 ]` is the primary gate. Argument scanning for
explicit flags is a secondary catch for cases where both fds are ttys but the
invocation is logically noninteractive (e.g. running `pi models` in a terminal
to inspect available models).

### Session Naming

Each invocation that proceeds through the Paseo path creates a new uniquely named
tmux session. The name format is:

```
pi-<timestamp>-<random4>
```

Where `<timestamp>` is seconds since epoch and `<random4>` is four hex digits from
`/dev/urandom`. Example: `pi-1755523812-a3f1`.

The session name is written to `~/.pi/agent/sessions/<name>.pid` along with the
PID of the Pi process inside tmux. This allows external tooling (e.g. `cmux top`)
to correlate the tmux session with a cmux surface without requiring integration
changes to cmux itself.

### Paseo Terminal Attachment

After the tmux session exists and the Pi pane is running, the shim calls the Paseo
relay to open a terminal window attached to the session:

```
POST $PASEO_RELAY_SOCK/terminal/open
{
  "type": "tmux-attach",
  "session": "<session-name>",
  "title": "pi (<session-name>)"
}
```

The relay responds synchronously with the terminal window ID or an error. On error,
the shim prints a clear warning to stderr and continues: the local tmux attach
proceeds regardless.

The Paseo terminal is opened with `tmux attach-session -t <session-name>` as its
command. Both the local terminal and the Paseo terminal are now full pty clients of
the same tmux session. Either can be used interactively.

### Argument Forwarding

All arguments passed to the shim are forwarded verbatim to the real Pi binary
inside tmux:

```sh
tmux new-session -d -s "$SESSION" -x "$COLS" -y "$ROWS" \
  "$PI_REAL" "$@"
```

The shim does not inspect, filter, or modify any arguments beyond the bypass
checks listed above. Environment variables are inherited unchanged from the
calling process. `PI_NO_PASEO` is unset in the child environment so it does not
propagate into Pi itself or any Pi-spawned subprocesses.

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

### Cleanup: Automatic Paseo Terminal Removal

The Paseo terminal must close when Pi exits. This is achieved without polling via
tmux hooks.

The shim registers a `set-hook` on the session before attaching:

```sh
tmux set-hook -t "$SESSION" pane-died \
  "run-shell '~/.pi/agent/bin/pi-cleanup $SESSION'"
```

`pi-cleanup` is a small helper that:

1. Calls `DELETE $PASEO_RELAY_SOCK/terminal/<window-id>` to close the Paseo pane.
2. Removes `~/.pi/agent/sessions/<name>.pid`.
3. Kills the tmux session if it is still alive (handles cases where Pi exits but
   the tmux session lingers due to `remain-on-exit` configuration).

The hook fires on the pane exit event, which tmux delivers immediately when the
Pi process terminates. There is no polling loop in either the shim or the cleanup
helper.

If `pi-cleanup` cannot reach the Paseo relay (e.g. relay was stopped first), it
logs the failure to `~/.pi/agent/sessions/<name>.cleanup.log` and skips the
relay call. The tmux session and pid file are still cleaned up.

### Emergency Bypass

Setting `PI_NO_PASEO=1` in the environment causes the shim to exec the real Pi
binary directly:

```sh
PI_NO_PASEO=1 pi
```

This bypass is silent (no warning printed). It is intended for:

- Debugging the shim itself without triggering Paseo.
- Running Pi in environments where Paseo is intentionally absent (CI, remote SSH
  without the relay, Docker containers).
- Recovering from a broken shim without modifying PATH.

`PI_REAL` can also be set to an explicit path to override the resolved real Pi
binary location. This is useful when testing a new Pi build alongside the
production installation.

### Fail-Clearly Behavior

The shim must never silently degrade. Each failure condition has a defined
observable output:

| Condition | Behavior |
|-----------|----------|
| tmux not installed | `pi-shim: tmux not found; launching pi directly` to stderr, then exec real Pi |
| tmux version too old (< 3.2) | `pi-shim: tmux <ver> is below required 3.2; launching pi directly` to stderr, then exec real Pi |
| Paseo relay socket missing | `pi-shim: paseo relay not found at <path>; launching pi directly` to stderr, then exec real Pi |
| Paseo relay connection refused | `pi-shim: paseo relay unreachable; launching pi directly` to stderr, then exec real Pi |
| tmux new-session fails | `pi-shim: failed to create tmux session; launching pi directly` to stderr, then exec real Pi |
| Paseo terminal open returns error | `pi-shim: paseo terminal open failed (<reason>); continuing without remote pane` to stderr, local attach proceeds |
| Real Pi binary not found | `pi-shim: real pi binary not found; check PI_REAL or ~/.pi/agent/pi-real` to stderr, exit 1 |

In every case where the shim falls back to a direct exec, Pi launches. The fallback
message goes to stderr so that interactive users see it and scripted callers can
suppress it with `2>/dev/null`.

## Lifecycle Summary

```
User types: pi [args]
  |
  v
~/.pi/agent/bin/pi (shim)
  |-- bypass checks (TMUX, PI_NO_PASEO, noninteractive)
  |-- prerequisite checks (tmux version, relay reachability, PI_REAL)
  |
  v (full path)
tmux new-session -d -s pi-<ts>-<rand> <PI_REAL> [args]
  |                                        |
  |                                        v
  |                                   Pi TUI running inside tmux session
  |
  v
POST paseo relay /terminal/open { type: tmux-attach, session: pi-<ts>-<rand> }
  |
  v
Paseo opens new terminal window running:
  tmux attach-session -t pi-<ts>-<rand>
  |
  v
shim: tmux attach-session -t pi-<ts>-<rand>  (local terminal joins)
  |
  v
[User interacts via either pane; both see identical TUI]
  |
  v
Pi exits
  |
  v
tmux fires pane-died hook
  |
  v
pi-cleanup: DELETE paseo /terminal/<window-id>
            rm ~/.pi/agent/sessions/pi-<ts>-<rand>.pid
            tmux kill-session -t pi-<ts>-<rand>
  |
  v
Paseo terminal closes. Local terminal returns to shell prompt.
```

## Caveats

### cmux / Tmux Resizing

When both the local pane and the Paseo pane are attached to the same tmux session,
tmux sizes the session to the SMALLER of the two terminal dimensions. This is
standard tmux multi-client behavior. If the Paseo terminal is narrower than the
local pane, the Pi TUI will appear constrained in the local view.

Mitigation: configure the Paseo terminal to open at the same dimensions as the
current local terminal. The shim reads `$COLUMNS` and `$LINES` from the calling
environment and passes them as the initial session size (`-x $COLS -y $ROWS`).
Subsequent resizes from either terminal update the session size dynamically.

For cmux users: the cmux surface layout determines the local pane size. The Pi
shim reads `CMUX_PANE_COLS` and `CMUX_PANE_ROWS` if set (populated by the cmux
spinup hook); otherwise it falls back to `$COLUMNS`/`$LINES` from the tty.

### Inline Image Support

Pi uses Kitty terminal graphics protocol (and/or Sixel) to render inline images in
the TUI. Tmux does not natively pass through inline image escape sequences. As a
result, inline images rendered by Pi will NOT appear correctly in either the local
pane or the Paseo pane when running through the shim.

This is a known limitation of the tmux approach. Approach B (PTY relay) also cannot
solve it without protocol-aware muxing. Users who require inline image support
should use `PI_NO_PASEO=1` to bypass the shim for those sessions.

If tmux gains native passthrough for Kitty graphics in a future version, this
caveat should be revisited.

### Security Implications

- **Session name predictability:** The session name `pi-<timestamp>-<rand4>` is
  not a secret. Any local user with access to `tmux list-sessions` can see active
  Pi sessions and their names. The random suffix is entropy-for-uniqueness, not
  entropy-for-secrecy. On multi-user machines, ensure tmux server socket
  permissions restrict access (`tmux -L <socket>` with a per-user socket).
- **Relay socket permissions:** `~/.pi/agent/paseo.sock` must be owner-only
  (`chmod 0600` or `0700`). The shim verifies this before sending the terminal-open
  request. If the socket is world-readable, the shim refuses to use it and falls
  back with a warning.
- **Argument forwarding:** The shim forwards all arguments verbatim to Pi. No
  injection surface is introduced that was not already present in direct Pi
  invocation. The shim itself must not be writable by any user other than the
  owner (`chmod 0755` with owner = current user; world-write is forbidden).
- **pi-cleanup helper:** The cleanup script is invoked by the tmux hook mechanism.
  Ensure `~/.pi/agent/bin/pi-cleanup` has the same ownership and permission
  requirements as the shim.

## Validation Expectations

The following checks constitute a passing validation of the integration:

1. **Shim is hit:** `which pi` returns `~/.pi/agent/bin/pi`. The real binary is
   reachable at the path recorded in `~/.pi/agent/pi-real`.
2. **Version forwarded correctly:** `pi --version` (noninteractive bypass) prints
   the Earendil 0.84.1 version string, not 0.70.2.
3. **Noninteractive bypass confirmed:** `pi --version` and `echo '' | pi --json`
   do not create any tmux sessions. `tmux list-sessions` is unchanged before and
   after each call.
4. **Interactive session creates tmux session:** Running `pi` in an interactive
   terminal creates exactly one new tmux session matching `pi-*`. `tmux
   list-sessions` shows it.
5. **Paseo pane appears:** The Paseo-managed terminal opens and shows the Pi TUI
   within 3 seconds of the tmux session being created.
6. **Both panes interactive:** Typing in either the local pane or the Paseo pane
   is reflected in both views immediately.
7. **Cleanup on exit:** Closing Pi (exit or `/quit`) causes the Paseo terminal to
   close automatically. `tmux list-sessions` no longer shows the session. The pid
   file under `~/.pi/agent/sessions/` is removed.
8. **PI_NO_PASEO bypass:** `PI_NO_PASEO=1 pi` launches Pi in the local terminal
   with no tmux session created and no Paseo terminal opened.
9. **Relay-absent fallback:** Stopping the Paseo relay and running `pi` produces
   a clear stderr warning and launches Pi locally with no tmux wrapping.
10. **Recursion guard:** Running `pi` from inside a tmux pane (with `TMUX` set)
    launches Pi directly without creating a nested tmux session.

## Open Items

- Confirm Paseo relay HTTP API surface (`/terminal/open`, `/terminal/<id>`) or
  whether a different IPC mechanism (Unix socket + line protocol) is used in
  Paseo 0.1.78. The design assumes HTTP-over-Unix-socket; adjust if the relay
  uses a different protocol.
- Decide whether `pi-cleanup` should use a timeout when calling the Paseo relay
  (recommended: 2 second timeout, then log and skip).
- Determine whether the agentfiles bootstrap should auto-start the Paseo relay
  as a launchd/systemd service or leave that to the user. The shim currently
  treats a missing relay as a graceful fallback rather than a fatal error.
- Inline image limitation: track upstream tmux development for passthrough support.
