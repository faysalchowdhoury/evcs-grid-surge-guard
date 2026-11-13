from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from evguard.api.app import create_app
from evguard.attacks import ATTACKS, hooks
from evguard.grid import Feeder
from evguard.guard import GridGuard, implausible_soc, meter_mismatch
from evguard.sim import RATED_KW, SITE_CAP_KW, Plaza, acceptance_kw, simulate_day

PLAZAS = [Plaza("A", 5, 3), Plaza("B", 10, 3)]
SITE_OF = np.repeat([0, 1], 3)


def _arrivals(rng: np.random.Generator, n: int = 30) -> list[list[tuple[int, float]]]:
    return [sorted((int(rng.integers(0, 1380)), float(rng.uniform(15, 50))) for _ in range(n)) for _ in PLAZAS]


def test_battery_acceptance_tapers() -> None:
    a = acceptance_kw(np.array([0.2, 0.55, 0.775, 1.0]), np.full(4, 200.0))
    assert a[0] == a[1] == 200.0 and a[2] == pytest.approx(200 * (1 - 0.425)) and a[3] == pytest.approx(30.0)


def test_simulation_respects_physics_and_site_cap() -> None:
    rng = np.random.default_rng(0)
    d = simulate_day(PLAZAS, _arrivals(rng), np.random.default_rng(1))
    assert (d.power <= RATED_KW + 1e-9).all() and (d.site_meter <= SITE_CAP_KW + 1e-6).all()
    same = (d.session[1:] == d.session[:-1]) & (d.session[1:] >= 0)
    assert np.allclose(d.site_meter.sum(1), d.power.sum(1)) and (np.diff(d.soc, axis=0)[same] >= 0).all()
    assert all(s["end"] > s["start"] for s in d.sessions) and (d.soc[d.connected] <= 0.8 + 0.05).all()


def test_attacks_change_the_right_layer() -> None:
    rng = np.random.default_rng(0)
    arr = _arrivals(rng, 60)
    base = simulate_day(PLAZAS, arr, np.random.default_rng(1))
    ramp = simulate_day(PLAZAS, arr, np.random.default_rng(1), **hooks("sync_ramp", 600, SITE_OF))
    assert ramp.limit[600:630][ramp.connected[600:630]].min() == RATED_KW  # site cap bypassed
    assert ramp.site_meter[600:630].sum() >= base.site_meter[600:630].sum()
    tam = simulate_day(PLAZAS, arr, np.random.default_rng(1), **hooks("meter_tamper", 600, SITE_OF, 0))
    assert np.allclose(tam.power, base.power) and tam.reported_power[600:660, :3].sum() == pytest.approx(0.5 * base.power[600:660, :3].sum())
    assert set(ATTACKS) == {"sync_ramp", "oscillation", "soc_spoof", "meter_tamper"}


def test_plausibility_rules() -> None:
    p = np.full((4, 2), 50.0)
    s = np.array([[0.30, 0.30], [0.31, 0.30], [0.32, 0.30], [0.33, 0.30]])
    c = np.ones((4, 2), bool)
    assert implausible_soc(p, s, c).tolist() == [False, True]  # connector 1 draws power but SoC never rises
    meter = np.array([[100.0], [100.0], [100.0]])
    assert meter_mismatch(meter, meter * 0.5).tolist() == [True] and meter_mismatch(meter, meter * 0.95).tolist() == [False]


def test_guard_fits_and_mitigates_sync_ramp() -> None:
    rng = np.random.default_rng(0)
    train = [simulate_day(PLAZAS, _arrivals(rng, 60), np.random.default_rng(k)) for k in range(4)]
    g = GridGuard.fit(train, SITE_OF)
    arr = _arrivals(rng, 60)
    hit = simulate_day(PLAZAS, arr, np.random.default_rng(9), **hooks("sync_ramp", 700, SITE_OF))
    g.reset(train[-1].site_meter.sum(1))
    guarded = simulate_day(PLAZAS, arr, np.random.default_rng(9), guard=g, **hooks("sync_ramp", 700, SITE_OF))
    assert any(k == "ramp" for t, k in g.alarms if 700 <= t < 730) or hit.site_meter[700:730].max() <= SITE_CAP_KW
    assert guarded.site_meter[701:730].max() <= max(SITE_CAP_KW, hit.site_meter[700:730].max()) + 1e-6
    assert (guarded.site_meter[702:730] <= SITE_CAP_KW + 1e-6).all()


def test_feeder_power_flow_responds_to_plaza_load() -> None:
    f = Feeder.build()
    lo = f.solve(18 * 60, np.zeros(3))
    hi = f.solve(18 * 60, np.full(3, 2700.0))
    assert hi["line_max_pct"] > lo["line_max_pct"] and hi["v_min_pu"] < lo["v_min_pu"]


def test_api_site_check() -> None:
    cl = TestClient(create_app())
    body = {"power_kw": [[50, 0], [50, 0], [50, 0], [50, 0]], "soc": [[0.3, 0.5], [0.3, 0.5], [0.3, 0.5], [0.3, 0.5]],
            "connected": [[True, False]] * 4, "meter_kw": [100, 100, 100, 100]}
    r = cl.post("/v1/site-check", json=body).json()
    assert r["implausible_connectors"] == [0] and r["meter_mismatch"] is True
    assert cl.post("/v1/site-check", json={**body, "soc": [[1.5, 0.5]] * 4}).status_code == 422
    _ = pd  # pandas is a runtime dependency of the package
