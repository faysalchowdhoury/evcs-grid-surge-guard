"""Experiment: benign training days, attack days (with and without the guard), feeder impact."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from .attacks import ATTACKS, DURATION, hooks  # noqa: E402
from .grid import Feeder  # noqa: E402
from .guard import GridGuard  # noqa: E402
from .sim import Plaza, load_sessions, sample_arrivals, simulate_day  # noqa: E402

PLAZAS = [Plaza("Plaza 1", 5), Plaza("Plaza 2", 10), Plaza("Plaza 3", 14)]
PER_DAY = 70.0
DETECTORS = ("ramp", "forecast", "telemetry", "reconciliation")


def _day(sessions, seed: int, **kw):  # type: ignore[no-untyped-def]
    rng = np.random.default_rng(seed)
    arrivals = [sample_arrivals(sessions, rng, PER_DAY) for _ in PLAZAS]
    return simulate_day(PLAZAS, arrivals, np.random.default_rng(seed + 10_000), **kw)


def run(sessions_csv: str, out_dir: str | Path, *, train_days: int = 21, attack_days: int = 8, benign_test_days: int = 7,
        seed: int = 0) -> dict[str, Any]:
    out = Path(out_dir)
    (out / "figures").mkdir(parents=True, exist_ok=True)
    sessions = load_sessions(sessions_csv)
    site_of = np.repeat(np.arange(len(PLAZAS)), [p.chargers for p in PLAZAS])
    t0 = time.perf_counter()
    train = [_day(sessions, seed + d) for d in range(train_days)]
    guard = GridGuard.fit(train, site_of, seed=seed)
    fit_s = time.perf_counter() - t0
    prev = train[-1].site_meter.sum(1)

    # false alarms on benign test days, guard active
    fp = {k: 0 for k in DETECTORS}
    mitig = 0
    for d in range(benign_test_days):
        guard.reset(prev)
        _day(sessions, seed + 500 + d, guard=guard)
        for _, k in guard.alarms:
            fp[k] += 1
        mitig += sum(k in ("ramp", "forecast") for _, k in guard.alarms)

    feeder = Feeder.build()
    rng = np.random.default_rng(seed + 7)
    rows: list[dict[str, Any]] = []
    example: dict[str, Any] = {}
    for d in range(attack_days):
        kind = ATTACKS[d % len(ATTACKS)]
        day_seed = seed + 900 + d
        base = _day(sessions, day_seed)
        # the attacker sees occupancy through the CSMS and strikes when the most cars are plugged in (16:30-19:30)
        busy = base.connected[16 * 60 + 30:19 * 60 + 30].sum(1)
        t_a = 16 * 60 + 30 + int(np.flatnonzero(busy == busy.max())[int(rng.integers(0, (busy == busy.max()).sum()))])
        h = hooks(kind, t_a, site_of, target_site=d % len(PLAZAS))
        hit = _day(sessions, day_seed, **h)
        guard.reset(prev)
        guarded = _day(sessions, day_seed, guard=guard, **h)
        alarms = [(t, k) for t, k in guard.alarms if t_a <= t < t_a + DURATION[kind] + 5]
        win = np.arange(t_a - 30, min(t_a + DURATION[kind] + 60, 1440))
        g = {name: feeder.series(win, r.site_meter[win]) for name, r in (("no_attack", base), ("attack", hit), ("attack_guarded", guarded))}
        row: dict[str, Any] = {"day": d, "kind": kind, "start_min": t_a, "connected_at_start": int(base.connected[t_a].sum()),
                               "detected_by": sorted({k for _, k in alarms}),
                               "ttd_min": (alarms[0][0] - t_a) if alarms else None}
        for name, r in (("no_attack", base), ("attack", hit), ("attack_guarded", guarded)):
            fleet = r.site_meter.sum(1)
            row[name] = {"peak_fleet_kw": float(fleet[win].max()), "max_swing_kw": float(np.abs(np.diff(fleet[win])).max()), "par": float(fleet.max() / max(fleet.mean(), 1e-9)),
                         "line_max_pct": float(g[name]["line_max_pct"].max()), "v_min_pu": float(g[name]["v_min_pu"].min()),
                         "trafo_max_pct": float(g[name]["trafo_max_pct"].max()),
                         "min_line_over_100": int((g[name]["line_max_pct"] > 100).sum())}
        rows.append(row)
        if kind == "oscillation" and not example:
            example = {"win": win, "base": base.site_meter[win].sum(1), "hit": hit.site_meter[win].sum(1),
                       "guarded": guarded.site_meter[win].sum(1), "t_a": t_a, "g": g}
    summary = {"plazas": len(PLAZAS), "chargers": int(len(site_of)), "train_days": train_days, "fit_s": fit_s,
               "thresholds": {"ramp_kw_per_min": guard.ramp_thr_kw, "simultaneous_limit_jumps": guard.jump_count_thr,
                              "forecast_residual_kw": guard.forecast_thr_kw},
               "benign_days": benign_test_days, "false_alarms": fp, "false_mitigations": mitig, "attacks": rows}
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _plot(example, out / "figures")
    write_report(summary, out / "RESULTS.md")
    return summary


def _plot(ex: dict[str, Any], fig_dir: Path) -> None:
    if not ex:
        return
    m = (ex["win"] - ex["t_a"])
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.2))
    ax = axes[0]
    ax.plot(m, ex["base"], color="0.5", label="no attack")
    ax.plot(m, ex["hit"], color="#c62828", label="oscillation attack, no guard")
    ax.plot(m, ex["guarded"], color="#2e7d32", label="with guard")
    ax.set_xlabel("minutes from attack start")
    ax.set_ylabel("fleet charging power (kW)")
    ax.set_title("Three plazas, 18 x 150-kW connectors")
    ax.legend(fontsize=8)
    ax = axes[1]
    for name, c in (("no_attack", "0.5"), ("attack", "#c62828"), ("attack_guarded", "#2e7d32")):
        ax.plot(m, ex["g"][name]["line_max_pct"], color=c, label=name.replace("_", " "))
    ax.axhline(100, color="k", ls="--", lw=0.8)
    ax.set_xlabel("minutes from attack start")
    ax.set_ylabel("most loaded feeder line (% of rating)")
    ax.set_title("CIGRE MV feeder (pandapower AC power flow)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(fig_dir / "surge.png", dpi=150)
    plt.close(fig)


def write_report(s: dict[str, Any], path: Path) -> Path:
    th = s["thresholds"]
    lines = ["# EV fast-charging surge guard results", "",
             f"{s['plazas']} plazas, {s['chargers']} connectors; guard fitted on {s['train_days']} benign days. Thresholds: ramp "
             f"{th['ramp_kw_per_min']:.0f} kW/min per plaza, {th['simultaneous_limit_jumps']} simultaneous limit jumps, forecast "
             f"residual {th['forecast_residual_kw']:.0f} kW.", "",
             f"## False alarms over {s['benign_days']} benign test days", "",
             "| Detector | Alarms |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in s["false_alarms"].items()]
    lines += [f"| grid mitigations triggered | {s['false_mitigations']} |", "",
              "## Attack days (evening peak)", "",
              "| Attack | Start (cars plugged in) | Detected by | Time to detect (min) | Peak fleet kW: none / attack / guarded | "
              "Largest 1-min swing kW: none / attack / guarded | Worst line loading %: none / attack / guarded | Min voltage pu |",
              "|---|---|---|---|---|---|---|---|"]
    for r in s["attacks"]:
        a, b, c = r["no_attack"], r["attack"], r["attack_guarded"]
        lines.append(f"| {r['kind']} | {r['start_min'] // 60:02d}:{r['start_min'] % 60:02d} ({r['connected_at_start']}/18) | "
                     f"{', '.join(r['detected_by']) or 'none'} | {'-' if r['ttd_min'] is None else r['ttd_min']} | "
                     f"{a['peak_fleet_kw']:.0f} / {b['peak_fleet_kw']:.0f} / {c['peak_fleet_kw']:.0f} | "
                     f"{a['max_swing_kw']:.0f} / {b['max_swing_kw']:.0f} / {c['max_swing_kw']:.0f} | "
                     f"{a['line_max_pct']:.0f} / {b['line_max_pct']:.0f} / {c['line_max_pct']:.0f} | "
                     f"{a['v_min_pu']:.3f} / {b['v_min_pu']:.3f} / {c['v_min_pu']:.3f} |")
    lines += ["", "![Surge](figures/surge.png)", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
