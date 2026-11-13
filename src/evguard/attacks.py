"""Attacks on a fast-charging network, applied through ``simulate_day`` hooks.

================  =====================================================================  ===========
attack            what the attacker does                                                 layer
================  =====================================================================  ===========
sync_ramp         compromised CSMS lifts every connector at every plaza to 150 kW at      control
                  the same minute for 30 min (site caps ignored)
oscillation       limits toggled between 0 and 150 kW every 2 min for 40 min, all plazas  control
soc_spoof         chargers at one plaza report a frozen 20% SoC for 60 min                telemetry
meter_tamper      chargers at one plaza under-report power by half for 60 min             telemetry
================  =====================================================================  ===========

Control attacks change what the grid sees; telemetry attacks corrupt what the operator sees.
"""

from __future__ import annotations

import numpy as np

from .sim import RATED_KW

ATTACKS = ("sync_ramp", "oscillation", "soc_spoof", "meter_tamper")
DURATION = {"sync_ramp": 30, "oscillation": 40, "soc_spoof": 60, "meter_tamper": 60}


def hooks(kind: str, t0: int, site_of: np.ndarray, target_site: int = 0):  # type: ignore[no-untyped-def]
    t1 = t0 + DURATION[kind]
    target = site_of == target_site

    def limit_override(t: int, limits: np.ndarray, conn: np.ndarray) -> np.ndarray:
        if not t0 <= t < t1:
            return limits
        if kind == "sync_ramp":
            return np.where(conn, RATED_KW, limits)
        on = ((t - t0) // 2) % 2 == 0
        return np.where(conn, RATED_KW if on else 0.0, limits)

    def telemetry(t: int, p: np.ndarray, soc: np.ndarray, conn: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if not t0 <= t < t1:
            return p, soc
        m = target & conn
        if kind == "soc_spoof":
            soc = np.where(m, 0.20, soc)
        else:
            p = np.where(m, 0.5 * p, p)
        return p, soc

    if kind in ("sync_ramp", "oscillation"):
        return {"limit_override": limit_override}
    return {"telemetry_tamper": telemetry}
