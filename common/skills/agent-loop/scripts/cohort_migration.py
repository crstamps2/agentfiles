"""Move legacy in-flight tickets into the durable cohort, with a metadata-only rollback.

Migration never touches Git, Jira, PRs, worktrees, or attempts. It derives admission order from
durable evidence, proposes the four oldest started tickets as members, parks the rest, and
synthesizes recovery contracts only where direct evidence names the action.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import tomllib

import cohort
import heal
import locks
import recovery
import state

PRE_GATE = frozenset(("spinup", "plan", "plan-review", "implement", "gates", "draft-pr", "ready", "bot-loop",
                      "blocked", "paused", "decision-required"))
SOURCE_RANK = {"cohort": 0, "parked": 1, "transcript": 2, "attempt": 2, "order": 3}
TRANSCRIPT_START = re.compile(r"^## (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) coordinator started", re.M)


class MigrationError(RuntimeError):
    pass


@dataclasses.dataclass
class Evidence:
    ticket: str
    source: str
    value: str
    detail: str


@dataclasses.dataclass
class MigrationPlan:
    generated_utc: str
    candidates: list[Evidence]
    members: list[str]
    parked: list[str]
    excluded: list[dict]
    recoveries: dict
    ambiguities: list[str]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tickets(cfg) -> list[state.Ticket]:
    root = cfg.state_root / "tickets"
    return [state.load(d) for d in sorted(root.iterdir()) if (d / "state.json").is_file()] if root.exists() else []


def _earliest_attempt(cfg, key) -> str:
    stamps = []
    for path in (cfg.state_root / "attempts" / key).glob("*/*/attempt.json"):
        try:
            started = json.loads(path.read_text()).get("started_utc")
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(started, str) and started:
            stamps.append(started)
    return min(stamps, default="")


def _evidence(cfg, t, members: dict, order: dict) -> Evidence:
    if t.key in members:
        return Evidence(t.key, "cohort", f"{members[t.key]:012d}", f"cohort admission #{members[t.key]}")
    meta = cfg.ticket_dir(t.key) / "PARKED.json"
    try:
        sequence = int(json.loads(meta.read_text())["admission_sequence"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        sequence = None
    if sequence is not None and sequence < 10**12:
        return Evidence(t.key, "parked", f"{sequence:012d}", f"parked admission #{sequence}")
    transcript = pathlib.Path(t.worktree or "/nonexistent") / "planning" / t.key.lower() / "transcript.md"
    try:
        starts = TRANSCRIPT_START.findall(transcript.read_text(errors="replace"))
    except OSError:
        starts = []
    if starts:
        return Evidence(t.key, "transcript", min(starts), f"coordinator started {min(starts)}")
    started = _earliest_attempt(cfg, t.key)
    if started:
        return Evidence(t.key, "attempt", started, f"earliest attempt {started}")
    return Evidence(t.key, "order", f"{order.get(t.key, len(order)):06d}:{t.key}", "configured order (no durable admission evidence)")


def _attempt_dir(cfg, key, task_id, entry) -> pathlib.Path | None:
    n = entry.get("n")
    path = cfg.state_root / "attempts" / key / task_id / str(n)
    return path if n is not None and path.is_dir() else None


def _accepted_tasks(cfg, t) -> list[tuple[str, dict, pathlib.Path]]:
    found = []
    for lineage, entries in sorted(t.attempts.items()):
        task_id = lineage.rsplit("/", 1)[-1]
        for entry in reversed(entries):
            if entry.get("outcome") == "accepted":
                adir = _attempt_dir(cfg, t.key, task_id, entry)
                try:
                    task = tomllib.loads((adir / "task.toml").read_text()) if adir else None
                except (OSError, tomllib.TOMLDecodeError):
                    task = None
                if task and task.get("slug"):
                    found.append((task_id, task, adir))
                break
    return found


def _missing_outputs(t, task: dict, adir: pathlib.Path) -> list[str]:
    """Allowlisted paths present in the accepted attempt's observed tree but absent now.

    A task that deleted a file (style migrations do) has no such path in its observed tree, so it
    is never mistaken for lost output."""
    try:
        observed = json.loads((adir / "attempt.json").read_text()).get("observed_tree") or ""
    except (OSError, json.JSONDecodeError):
        observed = ""
    if not observed:
        return []
    missing = []
    for path in task.get("allowed_files", []):
        if any(ch in path for ch in "*?[") or (pathlib.Path(t.worktree) / path).exists():
            continue
        present = subprocess.run(["git", "-C", t.worktree, "cat-file", "-e", f"{observed}:{path}"], capture_output=True)
        if present.returncode == 0:
            missing.append(path)
    return missing


def _landed_for(cfg, key) -> list[str]:
    out = []
    for path in sorted((cfg.state_root / "defects" / "landed").glob("*.json")):
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        refs = [row.get("ticket") for row in rec.get("affected_tickets", []) if isinstance(row, dict)]
        if rec.get("key") == key or key in refs:
            out.append(rec.get("id") or path.stem)
    return out


def _head(worktree) -> str:
    r = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _exhausted_tasks(cfg, t) -> list[tuple[str, str]]:
    """Unaccepted tasks whose last attempt failed, with the slug recorded by that attempt."""
    found = []
    for lineage, entries in sorted(t.attempts.items()):
        task_id = lineage.rsplit("/", 1)[-1]
        tries = [e for e in entries if e.get("n") is not None]
        if not tries or any(e.get("outcome") == "accepted" for e in tries):
            continue
        last = max(tries, key=lambda e: e["n"])
        adir = _attempt_dir(cfg, t.key, task_id, last)
        try:
            slug = tomllib.loads((adir / "task.toml").read_text()).get("slug") if adir else None
        except (OSError, tomllib.TOMLDecodeError):
            slug = None
        if slug and last.get("outcome") in ("rejected", "blocked", "interrupted", "environment", "protocol", "timeout"):
            found.append((task_id, slug))
    return found


def _supersedable(cfg, t) -> str | None:
    """An un-evented diagnostic wait created by the lifecycle may yield to direct evidence."""
    active = t.recovery.get("active") if isinstance(t.recovery, dict) else None
    if not active:
        return ""
    if (active.get("status") == "waiting-repair" and not active.get("last_event_id")
            and active.get("resume", {}).get("kind") in ("resume-stage", "replan")):
        return active.get("id")
    return None


def _synthesize(cfg, t) -> dict | None:
    """One recovery proposal backed by direct evidence, or a diagnostic contract. Never a decision."""
    if t.state != "blocked" and not (t.state == "paused" and _accepted_tasks(cfg, t)):
        return None
    if not t.worktree or not pathlib.Path(t.worktree).is_dir():
        return None
    supersedes = _supersedable(cfg, t)
    if supersedes is None:
        return None
    proposal = _direct(cfg, t)
    if proposal:
        if supersedes:
            proposal["supersedes"] = supersedes
        return proposal
    if supersedes:
        return None                                 # the existing diagnostic wait already covers this ticket
    return _diagnostic(t)


def _direct(cfg, t) -> dict | None:
    for task_id, task, adir in _accepted_tasks(cfg, t):
        missing = _missing_outputs(t, task, adir)
        if missing:
            slug = task["slug"]
            return {"checkpoint": {"state": "implement", "task_id": task_id, "task_slug": slug},
                    "resume": {"kind": "retry-task", "state": "implement", "task_id": task_id, "task_slug": slug},
                    "confidence": "direct", "status": "ready-to-resume",
                    "evidence": f"task {task_id} accepted in {adir.name}; outputs missing: {', '.join(missing)}"}
    landed = _landed_for(cfg, t.key)
    if t.state == "blocked" and landed:
        if t.reason.startswith("publish failed") and _accepted_tasks(cfg, t):
            return {"checkpoint": {"state": "implement"}, "resume": {"kind": "republish", "state": "implement"},
                    "confidence": "direct", "status": "ready-to-resume",
                    "evidence": f"publication failure; accepted work present; landed repair {landed[-1]}"}
        if t.previous == "gates" and (not t.head_sha or t.head_sha == _head(t.worktree)):
            return {"checkpoint": {"state": "gates", "head_sha": t.head_sha}, "resume": {"kind": "resume-stage", "state": "gates"},
                    "confidence": "direct", "status": "ready-to-resume",
                    "evidence": f"gate checkpoint unchanged; landed repair {landed[-1]}"}
        exhausted = _exhausted_tasks(cfg, t)
        if exhausted:
            task_id, slug = exhausted[-1]
            return {"checkpoint": {"state": "implement", "task_id": task_id, "task_slug": slug},
                    "resume": {"kind": "retry-task", "state": "implement", "task_id": task_id, "task_slug": slug},
                    "confidence": "direct", "status": "ready-to-resume",
                    "evidence": f"task {task_id} ladder exhausted; landed repair {landed[-1]}"}
    return None


def _diagnostic(t) -> dict | None:
    if t.state != "blocked":
        return None
    target = t.previous if t.previous in state.RESUMABLE_STATES else "implement"
    resume = {"kind": "replan", "state": "plan"} if target in ("plan", "plan-review") else {"kind": "resume-stage", "state": target}
    return {"checkpoint": {"state": "plan" if resume["kind"] == "replan" else target}, "resume": resume,
            "confidence": "diagnostic", "status": "waiting-repair",
            "evidence": f"legacy block without direct evidence: {t.reason[:160] or 'no reason'}"}


def build_plan(cfg) -> MigrationPlan:
    existing = {}
    if (cfg.state_root / "cohort.json").exists():
        existing = {row.ticket: row.admission_sequence for row in cohort.load(cfg).slots if row.ticket}
    order = {key: i for i, key in enumerate(cfg.tickets)}
    candidates, excluded, recoveries, ambiguities = [], [], {}, []
    for t in _tickets(cfg):
        if t.state not in PRE_GATE:
            if t.state in ("human-gate-1", "human-gate-2", "colleague-loop", "done"):
                ci = cfg.ticket_dir(t.key) / "ci.json"
                pr = json.loads(ci.read_text()).get("pr") if ci.exists() else None
                excluded.append({"ticket": t.key, "state": t.state, "pr": pr})
            continue
        ev = _evidence(cfg, t, existing, order)
        candidates.append(ev)
        if ev.source == "order":
            ambiguities.append(f"{t.key}: no durable admission evidence; using configured order")
        if not t.worktree or not pathlib.Path(t.worktree).is_dir():
            ambiguities.append(f"{t.key}: worktree missing; retained as a reservation for spinup retry")
        proposal = _synthesize(cfg, t)
        if proposal:
            recoveries[t.key] = proposal
            if proposal["confidence"] == "diagnostic":
                ambiguities.append(f"{t.key}: {proposal['evidence']}; diagnostic recovery, not a decision")
    candidates.sort(key=lambda e: (SOURCE_RANK[e.source], e.value, e.ticket))
    capacity = cfg.cohort_capacity
    members = [e.ticket for e in candidates[:capacity]]
    parked = [e.ticket for e in candidates[capacity:]]
    return MigrationPlan(_now(), candidates, members, parked, excluded, recoveries, ambiguities)


def render(plan: MigrationPlan) -> str:
    lines = [f"COHORT MIGRATION PLAN {plan.generated_utc}", "", "CHRONOLOGY"]
    lines += [f"{i}. {e.ticket}  {e.source}: {e.detail}" for i, e in enumerate(plan.candidates, 1)]
    lines += ["", "PROPOSED COHORT"] + [f"{i}  {key}" for i, key in enumerate(plan.members, 1)]
    lines += ["", "PARK"] + [f"{key} #{len(plan.members) + i}" for i, key in enumerate(plan.parked, 1)]
    lines += ["", "EXCLUDED (review-ready or later; never readmitted)"]
    lines += [f"{row['ticket']}  {row['state']}  PR #{row['pr']}" for row in plan.excluded]
    lines += ["", "RECOVERY"]
    lines += [f"{key}  {p['resume']['kind']}->{p['resume']['state']}  confidence={p['confidence']}  evidence: {p['evidence']}"
              for key, p in sorted(plan.recoveries.items())]
    lines += ["", "AMBIGUITIES"] + (plan.ambiguities or ["none"])
    return "\n".join(lines)


def _snapshot_dir(cfg, label: str) -> pathlib.Path:
    root = cfg.state_root / "cohort-migrations"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%S%fZ")
    path = root / f"{stamp}-{label}"
    path.mkdir(parents=True)
    return path


def _metadata(cfg, keys, supervisor) -> dict:
    cohort_path = cfg.state_root / "cohort.json"
    parked = {}
    for key in keys:
        tdir = cfg.ticket_dir(key)
        meta = tdir / "PARKED.json"
        parked[key] = {"marker": (tdir / "PARKED").exists(), "meta": meta.read_text() if meta.exists() else None}
    processed = cfg.state_root / "defects" / "events" / "processed"
    return {"cohort_json": cohort_path.read_text() if cohort_path.exists() else None, "parked": parked,
            "supervisors": sorted(supervisor.active()),
            "processed_events": sorted(p.name for p in processed.glob("*.json")) if processed.exists() else []}


def _write_manifest(path: pathlib.Path, manifest: dict) -> None:
    tmp = path / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=2))
    os.replace(tmp, path / "manifest.json")


def apply(cfg, plan: MigrationPlan, *, supervisor=None) -> pathlib.Path:
    import supervise
    supervisor = supervisor or supervise
    keys = [e.ticket for e in plan.candidates]
    with cohort._lease(cfg) as acquired:
        if not acquired:
            raise MigrationError("cohort lease is held; retry migration")
        if (cfg.state_root / "cohort.json").exists():
            raise MigrationError("cohort.json already exists; roll back the earlier migration first")
        snap = _snapshot_dir(cfg, "pre-migration")
        manifest = {"kind": "pre-migration", "created_utc": _now(), "plan": dataclasses.asdict(plan),
                    "before": _metadata(cfg, keys, supervisor), "recoveries_added": {}, "stopped": []}
        _write_manifest(snap, manifest)

        sequence = {key: i for i, key in enumerate(keys, 1)}
        slots = []
        for i in range(1, cfg.cohort_capacity + 1):
            key = plan.members[i - 1] if i <= len(plan.members) else None
            if key is None:
                slots.append(cohort.Slot(slot=i)); continue
            wt = state.load(cfg.ticket_dir(key)).worktree
            slots.append(cohort.Slot(slot=i, ticket=key, phase="active" if wt and pathlib.Path(wt).is_dir() else "reserved",
                                     admitted_utc=_now(), admission_sequence=sequence[key], source="migration",
                                     last_reconciled_utc=_now()))
        value = cohort.Cohort(schema_version=cohort.SCHEMA_VERSION, capacity=cfg.cohort_capacity, hard_capacity=cfg.hard_capacity,
                              generation=1, next_admission_sequence=len(keys) + 1, slots=slots,
                              history=[{"event": "migrated", "snapshot": snap.name, "utc": _now()}])
        cohort._save_unlocked(cfg, value)
        manifest["after_generation"] = value.generation
        _write_manifest(snap, manifest)

        for key, proposal in plan.recoveries.items():
            lease = locks.Lease(cfg.state_root / "locks" / f"runner-{key.lower()}", "cohort-migration")
            if not lease.acquire(hold=True):
                manifest.setdefault("skipped_recoveries", []).append(key); continue
            try:
                import repair
                summary = f"legacy block needs diagnosis: {proposal['evidence']}"
                defect_id = heal.signature(f"migration|{summary}") if proposal["status"] == "waiting-repair" else f"migration-{snap.name}"
                contract = recovery.new_contract(key, proposal["checkpoint"], proposal["resume"], (defect_id,), status="waiting-repair")
                if proposal["status"] == "waiting-repair":
                    repair.record_defect(cfg, key, "migration", summary, proposal["evidence"],
                                         recovery_ref={"ticket": key, "recovery_id": contract["id"],
                                                       "contract_fingerprint": recovery.fingerprint(contract)})
                t = state.load(cfg.ticket_dir(key))
                prior = t.recovery.get("active") if isinstance(t.recovery, dict) else None
                if proposal.get("supersedes") and prior and prior.get("id") == proposal["supersedes"]:
                    manifest.setdefault("superseded", {})[key] = prior
                    _write_manifest(snap, manifest)
                    t.recovery["active"] = None
                    state.save(cfg.ticket_dir(key), t)
                recovery.activate(cfg, key, contract)
                manifest["recoveries_added"][key] = contract["id"]
                _write_manifest(snap, manifest)
                if proposal["status"] == "ready-to-resume":
                    recovery.mark_ready(cfg, key, f"migration-{snap.name}")
            except recovery.RecoveryError as exc:
                manifest.setdefault("skipped_recoveries", []).append(f"{key}: {exc}")
                _write_manifest(snap, manifest)
            finally:
                lease.release()

        for key in plan.parked:
            tdir = cfg.ticket_dir(key)
            (tdir / "PARKED.json").write_text(json.dumps({"admission_sequence": sequence[key]}, indent=2))
            (tdir / "PARKED").touch()
        for key in plan.parked:
            if key in manifest["before"]["supervisors"]:
                supervisor.stop(key)
                manifest["stopped"].append(key)
        _write_manifest(snap, manifest)
    return snap


def rollback(cfg, snapshot_id: str, *, force: bool = False, supervisor=None) -> None:
    import supervise
    supervisor = supervisor or supervise
    snap = cfg.state_root / "cohort-migrations" / snapshot_id
    try:
        manifest = json.loads((snap / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"cannot read migration snapshot {snapshot_id}: {exc}") from exc
    before = manifest["before"]
    with cohort._lease(cfg) as acquired:
        if not acquired:
            raise MigrationError("cohort lease is held; retry rollback")
        current = cohort.load(cfg) if (cfg.state_root / "cohort.json").exists() else None
        processed = cfg.state_root / "defects" / "events" / "processed"
        events = sorted(p.name for p in processed.glob("*.json")) if processed.exists() else []
        later = []
        if current and current.generation != manifest.get("after_generation"):
            later.append(f"cohort generation {current.generation} != {manifest.get('after_generation')}")
        if set(events) - set(before["processed_events"]):
            later.append("recovery events were applied after migration")
        if later and not force:
            raise MigrationError("refusing metadata rollback: " + "; ".join(later) + " (use force to override)")
        if later:
            audit = _snapshot_dir(cfg, "pre-forced-rollback")
            _write_manifest(audit, {"kind": "pre-forced-rollback", "created_utc": _now(), "reasons": later,
                                    "before": _metadata(cfg, list(before["parked"]), supervisor)})

        cohort_path = cfg.state_root / "cohort.json"
        if before["cohort_json"] is None:
            cohort_path.unlink(missing_ok=True)
        else:
            cohort_path.write_text(before["cohort_json"])
        for key, prior in before["parked"].items():
            tdir = cfg.ticket_dir(key)
            if prior["meta"] is None:
                (tdir / "PARKED.json").unlink(missing_ok=True)
            else:
                (tdir / "PARKED.json").write_text(prior["meta"])
            if prior["marker"]:
                (tdir / "PARKED").touch()
            else:
                (tdir / "PARKED").unlink(missing_ok=True)
        for key, contract_id in manifest.get("recoveries_added", {}).items():
            tdir = cfg.ticket_dir(key)
            t = state.load(tdir)
            active = t.recovery.get("active") if isinstance(t.recovery, dict) else None
            if isinstance(active, dict) and active.get("id") == contract_id:
                t.recovery["active"] = manifest.get("superseded", {}).get(key)
                state.save(tdir, t)
        for key in manifest.get("stopped", []):
            supervisor.start(key, state.load(cfg.ticket_dir(key)).worktree, cfg.state_root)
