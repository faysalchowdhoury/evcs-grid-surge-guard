"""DC fast-charging plazas: sessions, battery physics, smart charging, OCPP-style telemetry.

**Demand.** Arrival times of day and energy needs are sampled from 35,377 real charging sessions
(12 residential sites in Norway, 2018-2021; Zenodo record 13896176). Those are home-charging sessions,
so this is the *shape* of real EV demand transplanted onto fast chargers, not measured fast-charging
behaviour: an assumption stated in the README.

**Battery.** Each arriving vehicle draws a pack (50-100 kWh) and a peak charge power (100-200 kW). It
accepts full power up to 55% state of charge and tapers linearly to 15% of peak at 100% (a CC-CV
approximation). Arrival SoC = 80% minus the sampled energy need; drivers leave at 80% or after 45 min.

**Control.** A charging station management system (CSMS) gives each connector a power limit
(OCPP ``SetChargingProfile``). Normally a site cap of 600 kW is shared in proportion to what each car can
accept. The physical power is ``min(limit, rated 150 kW, battery acceptance at true SoC)``. Attacks change
the *limits* (what a compromised CSMS can do) or the *telemetry* (what a tampered charger reports);
physics is always simulated from the true state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

RATED_KW = 150.0
SITE_CAP_KW = 600.0
TARGET_SOC = 0.8
MAX_STAY_MIN = 45
MIN_PER_DAY = 1440


def load_sessions(path: str | Path) -> pd.DataFrame:
    d = pd.read_csv(path, sep=";", decimal=",", parse_dates=["plugin_time"])
    d = d[(d["energy_session"] > 0.5) & (d["energy_session"] < 100)]
    return d.assign(hour=d["plugin_time"].dt.hour)[["hour", "energy_session"]].reset_index(drop=True)


def acceptance_kw(soc: np.ndarray, peak: np.ndarray) -> np.ndarray:
    taper = 1.0 - 0.85 * np.clip((soc - 0.55) / 0.45, 0, 1)
    return peak * taper


@dataclass
class Plaza:
    name: str
    bus: int
    chargers: int = 6


@dataclass
class DayResult:
    """Per-minute arrays, shape (1440, n_chargers) unless noted."""

    power: np.ndarray  # true power drawn (kW)
    soc: np.ndarray  # true SoC
    limit: np.ndarray  # limit in force at the connector (kW)
    connected: np.ndarray  # bool
    session: np.ndarray  # (1440, n) index of the charging session (OCPP transaction) per connector, -1 if idle
    reported_power: np.ndarray  # OCPP MeterValues as reported
    reported_soc: np.ndarray
    site_meter: np.ndarray  # (1440, n_sites) independent revenue meter at each plaza (kW)
    sessions: list[dict] = field(default_factory=list)


def sample_arrivals(sessions: pd.DataFrame, rng: np.random.Generator, per_day: float) -> list[tuple[int, float]]:
    """(minute of day, energy kWh) for one plaza-day, hour-of-day shape and energy taken from real sessions."""
    share = np.bincount(sessions["hour"], minlength=24) / len(sessions)
    out = []
    for h in range(24):
        for _ in range(rng.poisson(per_day * share[h])):
            row = sessions.iloc[int(rng.integers(len(sessions)))]
            out.append((h * 60 + int(rng.integers(60)), float(np.clip(row["energy_session"] * 2.0, 8.0, 60.0))))
    return sorted(out)


def simulate_day(plazas: list[Plaza], arrivals: list[list[tuple[int, float]]], rng: np.random.Generator, *,
                 limit_override=None, telemetry_tamper=None, guard=None) -> DayResult:
    """Minute-by-minute simulation of all plazas.

    ``limit_override(minute, limits, connected) -> limits`` lets an attacker rewrite CSMS limits;
    ``telemetry_tamper(minute, power, soc, connected) -> (power, soc)`` rewrites what chargers report;
    ``guard`` (optional) is called each minute with the telemetry and session ids so far and may return a dict of plaza
    index -> enforced (site cap kW, per-minute ramp kW) that overrides the CSMS.
    """
    n = sum(p.chargers for p in plazas)
    site_of = np.repeat(np.arange(len(plazas)), [p.chargers for p in plazas])
    P = np.zeros((MIN_PER_DAY, n))
    S = np.zeros((MIN_PER_DAY, n))
    L = np.zeros((MIN_PER_DAY, n))
    C = np.zeros((MIN_PER_DAY, n), bool)
    SS = np.full((MIN_PER_DAY, n), -1)
    RP = np.zeros((MIN_PER_DAY, n))
    RS = np.zeros((MIN_PER_DAY, n))
    meter = np.zeros((MIN_PER_DAY, len(plazas)))
    soc = np.zeros(n)
    cap = np.zeros(n)
    peak = np.zeros(n)
    left = np.zeros(n, int)
    active = np.full(n, -1)
    queue: list[list[tuple[int, float]]] = [list(a) for a in arrivals]
    sessions: list[dict] = []
    enforced: dict[int, tuple[float, float]] = {}
    prev_site = np.zeros(len(plazas))
    for t in range(MIN_PER_DAY):
        # departures and arrivals
        done = (soc >= TARGET_SOC) | (left <= 0)
        for k in np.flatnonzero(C[t - 1] & done) if t else []:
            sessions[active[k]]["end"] = t
            active[k] = -1
        conn = (C[t - 1] & ~done) if t else np.zeros(n, bool)
        for s_idx, q in enumerate(queue):
            while q and q[0][0] <= t:
                free = np.flatnonzero((site_of == s_idx) & ~conn)
                minute, energy = q.pop(0)
                if len(free) == 0:
                    continue  # all connectors busy: the driver goes elsewhere
                k = int(free[0])
                cap[k] = float(rng.choice([50.0, 64.0, 77.0, 100.0]))
                peak[k] = float(rng.choice([100.0, 150.0, 200.0]))
                soc[k] = max(0.05, TARGET_SOC - energy / cap[k])
                left[k] = MAX_STAY_MIN
                conn[k] = True
                active[k] = len(sessions)
                sessions.append({"charger": k, "site": s_idx, "start": t, "end": None, "cap_kwh": cap[k], "soc0": soc[k]})
        acc = np.where(conn, acceptance_kw(soc, peak), 0.0)
        # CSMS: share each site's cap in proportion to what each car can accept
        limits = np.zeros(n)
        for s_idx in range(len(plazas)):
            m = (site_of == s_idx) & conn
            want = np.minimum(acc[m], RATED_KW)
            tot = want.sum()
            limits[m] = want * min(1.0, SITE_CAP_KW / tot) if tot > 0 else 0.0
        local = limits.copy()  # what the plaza would do on its own
        if limit_override is not None:
            limits = limit_override(t, limits, conn)
        for s_idx, (cap_kw, ramp_kw) in enforced.items():  # local guard has the last word: CSMS commands ignored
            m = (site_of == s_idx) & conn
            limits[m] = local[m]
            allowed = min(cap_kw, prev_site[s_idx] + ramp_kw)
            tot = np.minimum(limits[m], acc[m]).sum()
            if tot > allowed > 0:
                limits[m] *= allowed / tot
        p = np.where(conn, np.minimum.reduce([limits, np.full(n, RATED_KW), acc]), 0.0)
        soc = np.where(conn, soc + p / 60.0 / np.maximum(cap, 1e-9), soc)
        left = np.where(conn, left - 1, left)
        P[t], S[t], L[t], C[t], SS[t] = p, soc, limits, conn, np.where(conn, active, -1)
        rp, rs = p.copy(), soc.copy()
        if telemetry_tamper is not None:
            rp, rs = telemetry_tamper(t, rp, rs, conn)
        RP[t], RS[t] = rp, rs
        meter[t] = np.bincount(site_of, weights=p, minlength=len(plazas))
        prev_site = meter[t]
        if guard is not None:
            enforced = guard(t, RP[: t + 1], RS[: t + 1], L[: t + 1], SS[: t + 1], meter[: t + 1]) or {}
    for s in sessions:
        if s["end"] is None:
            s["end"] = MIN_PER_DAY
    return DayResult(P, S, L, C, SS, RP, RS, meter, sessions)
