#!/usr/bin/env python3
"""resources.py — detect this machine's capacity and derive the concurrency ceiling.

WHY THIS EXISTS
---------------
"Use system resources efficiently" is meaningless without knowing what the machine has.
Hardcoding `max_workers = 8` is wrong twice: it over-subscribes an 8 GB Air and
under-uses a 128 GB Studio. And the binding constraint is rarely CPU — the real limits
are unified memory (local models), provider rate limits and the budget ceiling.

So this module measures, then derives a *starting* ceiling that the scheduler refines at
runtime from observed queue wait and retry rates.

DESIGN
------
- **Detect, never assume.** CPU count and physical memory come from the OS. When
  detection fails the answer is a conservative one (2 workers), not a hopeful one.
- **Unified memory is the real constraint on Apple Silicon.** GPU and CPU share the
  same pool, so a local model that does not fit causes system-wide swap. The ceiling is
  therefore lowered when local models are in play.
- **Thermal and power state are considered.** A Mac on battery in low-power mode
  throttles; continuing at full fan-out produces timeouts, not throughput.
- **Pure stdlib.** `os`, `sys`, `subprocess`-free where possible, so the engine keeps
  its "no runtime dependency" property and works on a fresh machine.

Usage:
    caps = detect()
    ceiling = derive_ceiling(caps, cfg, local_models_in_use=2)
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

__all__ = ["MachineCaps", "detect", "derive_ceiling", "estimate_local_model_footprint"]


@dataclass(frozen=True)
class MachineCaps:
    """What this machine can do, as measured rather than assumed."""

    cpu_count: int
    physical_memory_bytes: int
    platform: str
    machine: str
    is_apple_silicon: bool
    thermal_state: str | None = None      # nominal | fair | serious | critical | None
    low_power_mode: bool | None = None
    battery_powered: bool | None = None

    @property
    def memory_gb(self) -> float:
        """Physical memory in GiB, rounded for display."""
        return round(self.physical_memory_bytes / (1024 ** 3), 1)

    @property
    def on_battery(self) -> bool:
        """True when running on battery power, when that could be determined."""
        return bool(self.battery_powered)

    def throttled(self) -> bool:
        """True when the OS reports a state that argues for reducing concurrency."""
        if self.thermal_state in ("serious", "critical"):
            return True
        return bool(self.low_power_mode)

    def as_dict(self) -> dict[str, Any]:
        """Serialisable summary for events and the resources view."""
        return {
            "cpu_count": self.cpu_count,
            "memory_gb": self.memory_gb,
            "platform": self.platform,
            "machine": self.machine,
            "apple_silicon": self.is_apple_silicon,
            "thermal_state": self.thermal_state,
            "low_power_mode": self.low_power_mode,
            "on_battery": self.on_battery,
            "throttled": self.throttled(),
        }


def _sysctl(name: str) -> str | None:
    """Read one sysctl value, or None when unavailable.

    Guarded because this is a macOS convenience, not a requirement: on Linux or in a
    sandbox the engine must still start with sensible defaults.
    """
    binary = shutil.which("sysctl")
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "-n", name], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value or None


def _thermal_state() -> str | None:
    """Read the macOS thermal pressure level, mapped to a readable word.

    `pmset -g therm` reports `CPU_Scheduler_Limit` and a pressure level; a limit below
    100 means the OS is already throttling us, which is exactly when adding workers
    makes things worse.
    """
    binary = shutil.which("pmset")
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "-g", "therm"], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout
    if "CPU_Scheduler_Limit" not in text:
        return None
    try:
        limit = int(text.split("CPU_Scheduler_Limit")[1].split("=")[1].split()[0])
    except (IndexError, ValueError):
        return None
    if limit >= 100:
        return "nominal"
    if limit >= 80:
        return "fair"
    if limit >= 50:
        return "serious"
    return "critical"


def _low_power_mode() -> bool | None:
    """Read macOS Low Power Mode, when `pmset` reports it."""
    binary = shutil.which("pmset")
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "-g"], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        if "lowpowermode" in line.lower():
            try:
                return bool(int(line.split()[-1]))
            except (IndexError, ValueError):
                return None
    return None


def _battery_powered() -> bool | None:
    """True when on battery. `pmset -g batt` prints 'Battery Power' or 'AC Power'."""
    binary = shutil.which("pmset")
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "-g", "batt"], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.lower()
    if "ac power" in text:
        return False
    if "battery power" in text:
        return True
    return None


def detect() -> MachineCaps:
    """Measure the current machine's capacity.

    Every probe degrades gracefully: an unavailable value stays ``None`` rather than
    becoming a fabricated number, so a caller can distinguish "not throttled" from
    "could not tell".
    """
    machine = platform.machine()
    is_apple = sys.platform == "darwin" and machine in ("arm64", "aarch64")

    cpu = os.cpu_count() or 1
    if is_apple:
        hw = _sysctl("hw.perflevel0.logicalcpu") or _sysctl("hw.ncpu")
        if hw and hw.isdigit():
            cpu = max(cpu, int(hw))

    memory = 0
    if is_apple:
        mem = _sysctl("hw.memsize")
        if mem and mem.isdigit():
            memory = int(mem)
    if memory <= 0:
        try:
            memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError, AttributeError):
            memory = 8 * (1024 ** 3)  # conservative fallback, not a claim

    return MachineCaps(
        cpu_count=max(1, cpu),
        physical_memory_bytes=max(0, memory),
        platform=platform.system(),
        machine=machine or "unknown",
        is_apple_silicon=is_apple,
        thermal_state=_thermal_state() if sys.platform == "darwin" else None,
        low_power_mode=_low_power_mode() if sys.platform == "darwin" else None,
        battery_powered=_battery_powered() if sys.platform == "darwin" else None,
    )


# A local model's resident footprint is dominated by weights plus the KV cache. These
# are deliberately rough: the point is to avoid launching three 7B models on a 16 GB
# machine, not to predict memory to the megabyte.
_PARAM_MARKERS = (
    ("0.5b", 0.4), ("1b", 0.8), ("1.5b", 1.1), ("3b", 2.2), ("7b", 5.0),
    ("8b", 5.6), ("13b", 9.0), ("14b", 9.5), ("20b", 13.0), ("30b", 19.0),
    ("32b", 20.0), ("70b", 42.0), ("72b", 44.0),
)
# KV-cache headroom per concurrent local model, in GiB. Unified memory means this
# competes directly with the rest of the OS, which is why it is generous.
_KV_HEADROOM_GB = 1.5


def estimate_local_model_footprint(model_id: str) -> float:
    """Rough GiB a local model occupies, inferred from its name.

    Returning a *conservative* estimate is intentional: under-estimating causes swap,
    which is far more damaging than running one fewer model in parallel.
    """
    lowered = model_id.lower()
    best = 4.0  # unknown size: assume a mid-size model rather than pretending it is tiny
    for marker, gb in _PARAM_MARKERS:
        if marker in lowered:
            best = gb
            break
    # Quantised variants are smaller; a q4 GGUF is roughly 55% of fp16 weights.
    if any(q in lowered for q in ("q4", "4bit", "int4")):
        best *= 0.55
    elif any(q in lowered for q in ("q5", "5bit")):
        best *= 0.68
    elif any(q in lowered for q in ("q8", "8bit")):
        best *= 1.0
    return round(best + _KV_HEADROOM_GB, 2)


def derive_ceiling(caps: MachineCaps, *, cpu_headroom: int = 1,
                   configured_ceiling: int | None = None,
                   local_models_in_use: int = 0,
                   local_model_ids: list[str] | None = None,
                   reserve_memory_gb: float = 4.0) -> dict[str, Any]:
    """Derive a starting concurrency ceiling and explain how it was reached.

    The explanation matters as much as the number: "why is my org only running two
    agents?" should be answerable from the resources view, not a mystery.

    Returns a dict with ``ceiling``, ``cpu_bound``, ``memory_bound``, ``reason`` and the
    inputs, so the UI can show the derivation.
    """
    cpu_bound = max(1, caps.cpu_count - max(0, cpu_headroom))
    reasons: list[str] = []
    ceiling = cpu_bound

    # Memory: total minus a reserve for the OS and the app, divided by the footprint of
    # the local models actually in play. Only local models count — cloud inference costs
    # us no memory.
    memory_bound: int | None = None
    if local_models_in_use > 0 and caps.physical_memory_bytes > 0:
        available_gb = max(0.0, caps.memory_gb - reserve_memory_gb)
        ids = local_model_ids or []
        footprint = estimate_local_model_footprint(ids[0]) if ids else 5.0
        if footprint > 0:
            memory_bound = max(1, int(available_gb // footprint))
            ceiling = min(ceiling, memory_bound)
            reasons.append(
                f"memory: {available_gb:.1f} GiB usable / ~{footprint:.1f} GiB per local "
                f"model = {memory_bound}"
            )
    elif local_models_in_use > 0:
        # Could not read memory but local models are in play: be conservative, because
        # guessing high here causes swap, not merely slowness.
        memory_bound = 1
        ceiling = 1
        reasons.append("memory: unreadable with local models in use; capping at 1")

    if caps.throttled():
        ceiling = max(1, ceiling // 2)
        reasons.append(f"thermal/power throttling ({caps.thermal_state or 'low-power'}): halved")

    if caps.on_battery and caps.is_apple_silicon:
        # Not a hard throttle — just a hint that throughput is less valuable than
        # battery life and responsiveness right now.
        reasons.append("on battery: consider reducing fan-out")

    if configured_ceiling is not None:
        if configured_ceiling < ceiling:
            reasons.append(f"configured ceiling {configured_ceiling} is lower; honouring it")
        else:
            reasons.append(f"configured ceiling {configured_ceiling} is higher; ignored (unsafe)")
        ceiling = min(ceiling, configured_ceiling)

    return {
        "ceiling": max(1, ceiling),
        "cpu_count": caps.cpu_count,
        "cpu_bound": cpu_bound,
        "memory_bound": memory_bound,
        "memory_gb": caps.memory_gb,
        "local_models_in_use": local_models_in_use,
        "throttled": caps.throttled(),
        "reason": "; ".join(reasons) if reasons else "cpu-bound only",
    }
