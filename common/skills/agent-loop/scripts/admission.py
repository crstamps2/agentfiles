"""Machine-pressure probes and the admission decision for heavy stages."""
from __future__ import annotations
import dataclasses
import re
import subprocess


@dataclasses.dataclass(frozen=True)
class Reading:
    compressor_pct: float
    load1: float
    cores: int
    on_ac: bool
    therm_limited: bool
    disk_free_gb: float


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


def parse_vm_stat(text: str, memsize_bytes: int) -> float:
    page = re.search(r"page size of (\d+) bytes", text)
    comp = re.search(r"Pages occupied by compressor:\s+(\d+)", text)
    if not (page and comp and memsize_bytes):
        return 0.0
    return int(comp.group(1)) * int(page.group(1)) / memsize_bytes * 100.0


def parse_uptime(text: str) -> float:
    m = re.search(r"load averages?:\s*([\d.]+)", text)
    return float(m.group(1)) if m else 0.0


def parse_batt(text: str) -> bool:
    return "AC Power" in text


def parse_therm(text: str) -> bool:
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", text)
    return bool(m) and int(m.group(1)) < 100


def parse_df(text: str) -> float:
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return 0.0
    parts = lines[1].split()
    try:
        return int(parts[3]) / (1024 ** 2)   # 1024-blocks → GB
    except (IndexError, ValueError):
        return 0.0


def probe(state_root) -> Reading:
    memsize = int((_out(["sysctl", "-n", "hw.memsize"]) or "0").strip() or 0)
    cores = int((_out(["sysctl", "-n", "hw.ncpu"]) or "1").strip() or 1)
    return Reading(
        compressor_pct=parse_vm_stat(_out(["vm_stat"]), memsize),
        load1=parse_uptime(_out(["uptime"])),
        cores=cores,
        on_ac=parse_batt(_out(["pmset", "-g", "batt"])),
        therm_limited=parse_therm(_out(["pmset", "-g", "therm"])),
        disk_free_gb=parse_df(_out(["df", "-k", str(state_root)])),
    )


def decide(r: Reading, th) -> Decision:
    reasons = []
    if r.compressor_pct > th.compressor_pct_max:
        reasons.append(f"compressor {r.compressor_pct:.1f}% > {th.compressor_pct_max}%")
    if r.cores and r.load1 / r.cores > th.load_per_core_max:
        reasons.append(f"load {r.load1:.2f} on {r.cores} cores > {th.load_per_core_max}/core")
    if th.require_ac_power and not r.on_ac:
        reasons.append("on battery power")
    if r.therm_limited:
        reasons.append("thermal: CPU speed limited")
    if r.disk_free_gb < th.disk_free_gb_min:
        reasons.append(f"disk free {r.disk_free_gb:.1f}GB < {th.disk_free_gb_min}GB")
    return Decision(ok=not reasons, reasons=reasons)
