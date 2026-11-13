"""Site-level guard: detectors fitted on benign days, and the local mitigation they trigger.

Detectors (all thresholds from benign training days; nothing is fitted on attacks):

* ``ramp``: a plaza's metered power rises in one minute by more than 1.1 x the largest rise seen in the
  benign training days, or more connectors fleet-wide get a limit increase > 50 kW in the same minute
  than ever seen in training;
* ``forecast``: an LSTM predicts next-minute fleet power from the last hour and time of day; two
  consecutive residuals above the benign 99.9th percentile x 1.2 raise an alarm;
* ``telemetry``: a charger reports power while its SoC does not rise for 3 minutes, or SoC jumps by more
  than 10 points in a minute;
* ``reconciliation``: a plaza's independent revenue meter differs from the sum of its chargers' reported
  power by more than 10% + 15 kW for 3 minutes.

Mitigation: a grid alarm (ramp or forecast) puts the plazas in **local mode** for 60 min: CSMS commands
are ignored, each plaza shares its normal 600-kW cap itself and ramps by at most 100 kW/min. (An early
version only rate-limited CSMS commands; against the oscillation attack that made line loading worse.) Telemetry and
reconciliation alarms are operator alerts (quarantine the charger or site data), not grid actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import torch
from torch import nn

from .sim import MIN_PER_DAY, SITE_CAP_KW, DayResult

SEQ = 60
SCALE = 2000.0


class Forecaster(nn.Module):
    def __init__(self, hidden: int = 32) -> None:
        super().__init__()
        self.lstm = nn.LSTM(3, hidden, batch_first=True)
        self.out = nn.Linear(hidden, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h, _ = self.lstm(x)
        return self.out(h[:, -1]).squeeze(-1)


def _features(total_kw: np.ndarray) -> np.ndarray:
    m = np.arange(len(total_kw))
    ang = 2 * np.pi * (m % MIN_PER_DAY) / MIN_PER_DAY
    return np.c_[total_kw / SCALE, np.sin(ang), np.cos(ang)].astype(np.float32)


def train_forecaster(days: list[DayResult], *, epochs: int = 4, seed: int = 0) -> tuple[Forecaster, np.ndarray]:
    torch.manual_seed(seed)
    series = np.concatenate([d.site_meter.sum(1) for d in days])
    f = _features(series)
    idx = np.arange(SEQ, len(series))
    x = torch.tensor(np.stack([f[i - SEQ:i] for i in idx]))
    y = torch.tensor(series[idx] / SCALE, dtype=torch.float32)
    model = Forecaster()
    opt = torch.optim.Adam(model.parameters(), lr=3e-3)
    for _ in range(epochs):
        for b in torch.randperm(len(x)).split(256):
            opt.zero_grad()
            loss = ((model(x[b]) - y[b]) ** 2).mean()
            loss.backward()
            opt.step()
    with torch.no_grad():
        res = np.abs(model(x).numpy() - y.numpy()) * SCALE
    return model, res


def implausible_soc(rp: np.ndarray, rs: np.ndarray, conn: np.ndarray) -> np.ndarray:
    """Per connector, over the last 4 minutes (rows): power reported every minute while SoC never rises,
    or SoC jumping by more than 10 points in the last minute. ``conn`` marks minutes that belong to the
    connector's *current* transaction: a new car on the same connector legitimately changes SoC."""
    charging = (rp > 5.0).all(0) & conn.all(0)
    stalled = (np.diff(rs, axis=0) <= 0).all(0)
    jump = (np.abs(rs[-1] - rs[-2]) > 0.10) & conn[-1] & conn[-2]
    return (charging & stalled) | jump


def meter_mismatch(meter: np.ndarray, reported_sum: np.ndarray) -> np.ndarray:
    """Per site, over the last 3 minutes (rows): revenue meter and summed charger reports disagree by more
    than 10% + 15 kW every minute."""
    return (np.abs(meter - reported_sum) > 0.1 * meter + 15.0).all(0)


@dataclass
class GridGuard:
    site_of: np.ndarray
    ramp_thr_kw: float
    jump_count_thr: int
    forecaster: Forecaster
    forecast_thr_kw: float
    mitigate: bool = True
    hold_min: int = 60
    alarms: list[tuple[int, str]] = field(default_factory=list)
    _history: np.ndarray | None = None
    _until: int = -1
    _streak: dict[str, int] = field(default_factory=dict)

    @classmethod
    def fit(cls, days: list[DayResult], site_of: np.ndarray, *, seed: int = 0) -> "GridGuard":
        ramps = np.concatenate([np.diff(d.site_meter, axis=0).ravel() for d in days])
        jumps = max(int((np.diff(d.limit, axis=0) > 50.0).sum(1).max()) for d in days)
        model, res = train_forecaster(days, seed=seed)
        return cls(site_of, 1.1 * float(ramps.max()), jumps + 1, model, 1.2 * float(np.quantile(res, 0.999)))

    def reset(self, history: np.ndarray | None = None) -> None:
        """Start a new day; ``history`` is the previous day's fleet power (for the forecaster's first hour)."""
        self.alarms, self._until, self._streak = [], -1, {}
        self._history = history

    def _flag(self, t: int, kind: str, need: int, cond: bool) -> bool:
        self._streak[kind] = self._streak.get(kind, 0) + 1 if cond else 0
        if self._streak[kind] == need:
            self.alarms.append((t, kind))
            return True
        return False

    @torch.no_grad()
    def __call__(self, t: int, rp: np.ndarray, rs: np.ndarray, lim: np.ndarray, sess: np.ndarray, meter: np.ndarray):  # type: ignore[no-untyped-def]
        grid_alarm = False
        if t >= 1:
            ramp = float((meter[t] - meter[t - 1]).max())
            jumps = int(((lim[t] - lim[t - 1]) > 50.0).sum())
            grid_alarm |= self._flag(t, "ramp", 1, ramp > self.ramp_thr_kw or jumps >= self.jump_count_thr)
        total = meter.sum(1)
        hist = total if self._history is None else np.r_[self._history[-SEQ:], total]
        if len(hist) > SEQ:
            f = _features(hist)[-SEQ - 1:-1]
            ang = 2 * np.pi * (np.arange(t - SEQ, t) % MIN_PER_DAY) / MIN_PER_DAY
            f[:, 1], f[:, 2] = np.sin(ang), np.cos(ang)
            pred = float(self.forecaster(torch.tensor(f[None]))[0]) * SCALE
            grid_alarm |= self._flag(t, "forecast", 2, abs(total[-1] - pred) > self.forecast_thr_kw)
        if t >= 3:
            same = (sess[t - 3:t + 1] == sess[t][None]) & (sess[t][None] >= 0)  # same OCPP transaction
            self._flag(t, "telemetry", 1, bool(implausible_soc(rp[t - 3:t + 1], rs[t - 3:t + 1], same).any()))
            rep = np.vstack([np.bincount(self.site_of, weights=rp[k], minlength=meter.shape[1]) for k in range(t - 2, t + 1)])
            self._flag(t, "reconciliation", 1, bool(meter_mismatch(meter[t - 2:t + 1], rep).any()))
        if grid_alarm and self.mitigate:
            self._until = t + self.hold_min
        if t <= self._until:
            return {s: (SITE_CAP_KW, 100.0) for s in range(meter.shape[1])}
        return {}
