"""Machine-pressure probes and the admission decision for heavy stages."""
from __future__ import annotations
import dataclasses
import math
import re
import subprocess


@dataclasses.dataclass(frozen=True)
class Reading:
    compressor_pct: float | None
    load1: float | None
    cores: int
    on_ac: bool
    therm_limited: bool | None
    disk_free_gb: float | None
    pressure_level: str | None = None
    thermal_level: int | None = None
    thermal_state: int | None = None


@dataclasses.dataclass(frozen=True)
class Decision:
    ok: bool
    reasons: list


def _out(argv: list) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def parse_vm_stat(text: str, memsize_bytes: int) -> float | None:
    page = re.search(r"page size of (\d+) bytes", text)
    comp = re.search(r"Pages occupied by compressor:\s+(\d+)", text)
    if not (page and comp and memsize_bytes):
        return None
    return int(comp.group(1)) * int(page.group(1)) / memsize_bytes * 100.0


def parse_uptime(text: str) -> float | None:
    m = re.search(r"load averages?:\s*([\d.]+)", text)
    return float(m.group(1)) if m else None


def parse_batt(text: str) -> bool:
    return "AC Power" in text


def parse_therm(text: str) -> bool | None:
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", text)
    if not m:
        return None
    return int(m.group(1)) < 100


def parse_thermal_state(text: str) -> int | None:
    """Parse NSProcessInfo.thermalState from osascript output. Valid range: 0-3."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        val = int(text)
        return val if 0 <= val <= 3 else None
    except ValueError:
        return None


def parse_memory_pressure(text: str) -> str | None:
    text = text or ""
    low = text.lower()
    for level in ("critical", "warn", "normal"):
        if re.search(rf"\b{level}\b", low):
            return level
    m = re.search(r"System-wide memory free percentage:\s*(\d+(?:\.\d+)?)%", text, re.I)
    if not m:
        return None
    free = float(m.group(1))
    return "critical" if free < 5 else "warn" if free < 10 else "normal"


def parse_df(text: str) -> float:
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return float("nan")
    # Scan all lines after the header for the first line with 4+ fields where fields 1-3 are integers
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 4:
            # Try: device, total, used, available, ... (normal case: fields 1-3 are integers)
            try:
                int(parts[1]); int(parts[2]); int(parts[3])  # Validate fields 1-3 are integers
                return int(parts[3]) / (1024 ** 2)   # 1024-blocks → GB
            except (ValueError, IndexError):
                pass
            # Try: total, used, available, ... (wrapped device case: fields 0-2 are integers)
            try:
                int(parts[0]); int(parts[1]); int(parts[2])  # Validate fields 0-2 are integers
                return int(parts[2]) / (1024 ** 2)   # 1024-blocks → GB
            except (ValueError, IndexError):
                continue
    return float("nan")


def probe(state_root) -> Reading:
    memsize = int((_out(["sysctl", "-n", "hw.memsize"]) or "0").strip() or 0)
    cores = int((_out(["sysctl", "-n", "hw.ncpu"]) or "1").strip() or 1)
    vm, up = _out(["vm_stat"]), _out(["uptime"])
    disk, pressure = _out(["df", "-k", str(state_root)]), _out(["memory_pressure"])
    disk_free = parse_df(disk)
    thermal_str = (_out(["sysctl", "-n", "machdep.xcpm.cpu_thermal_level"]) or "").strip()
    thermal_level = None
    if thermal_str:
        try:
            thermal_level = int(thermal_str)
        except ValueError:
            pass
    thermal_state = parse_thermal_state(_out(["osascript", "-l", "JavaScript", "-e", "ObjC.import('Foundation'); $.NSProcessInfo.processInfo.thermalState"]))
    return Reading(
        compressor_pct=parse_vm_stat(vm, memsize) if vm and memsize else None,
        load1=parse_uptime(up) if up else None,
        cores=cores,
        on_ac=parse_batt(_out(["pmset", "-g", "batt"])),
        therm_limited=parse_therm(_out(["pmset", "-g", "therm"])),
        disk_free_gb=None if math.isnan(disk_free) else disk_free,
        pressure_level=parse_memory_pressure(pressure),
        thermal_level=thermal_level,
        thermal_state=thermal_state,
    )


def decide(r: Reading, th) -> Decision:
    reasons = []
    # defer timing is wired by the Plan 3 tick.
    if r.compressor_pct is None:
        reasons.append("compressor: unknown")
    elif r.compressor_pct > th.compressor_pct_max:
        reasons.append(f"compressor {r.compressor_pct:.1f}% > {th.compressor_pct_max}%")
    if r.load1 is None:
        reasons.append("load: unknown")
    elif r.cores and r.load1 / r.cores > th.load_per_core_max:
        reasons.append(f"load {r.load1:.2f} on {r.cores} cores > {th.load_per_core_max}/core")
    if r.pressure_level is None:
        reasons.append("pressure: unknown")
    elif r.pressure_level in {"warn", "critical"}:
        reasons.append(f"pressure: {r.pressure_level}")
    if th.require_ac_power and not r.on_ac:
        reasons.append("on battery power")
    if r.therm_limited is True:
        reasons.append("thermal: CPU speed limited")
    elif r.thermal_state is not None:
        if r.thermal_state >= 2:
            reasons.append(f"thermal: NSProcessInfo state {r.thermal_state}")
    elif r.thermal_level is not None:
        if r.thermal_level > 0:
            reasons.append(f"thermal: xcpm level {r.thermal_level}")
    elif r.therm_limited is None and r.thermal_state is None and r.thermal_level is None:
        reasons.append("thermal: unknown")
    if r.disk_free_gb is None or (isinstance(r.disk_free_gb, float) and math.isnan(r.disk_free_gb)):
        reasons.append("disk: unknown")
    elif r.disk_free_gb < th.disk_free_gb_min:
        reasons.append(f"disk free {r.disk_free_gb:.1f}GB < {th.disk_free_gb_min}GB")
    return Decision(ok=not reasons, reasons=reasons)
