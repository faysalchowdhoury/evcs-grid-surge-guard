"""Telemetry plausibility API for a charging operator's data pipeline.

``POST /v1/site-check``  the last 4 minutes of one plaza: each connector's reported power (kW) and SoC
                         (0-1) from OCPP MeterValues, whether it is connected, and the plaza's independent
                         revenue-meter reading. Returns connectors with implausible SoC behaviour and
                         whether the meter disagrees with the sum of reports.
``GET  /health``

The grid-side detectors (ramp, LSTM forecast) and the mitigation run inside the plaza controller loop
(``GridGuard``), where they can act within a minute; they are not exposed over HTTP.
"""

from __future__ import annotations

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .. import __version__
from ..guard import implausible_soc, meter_mismatch


class SiteWindow(BaseModel):
    power_kw: list[list[float]] = Field(description="(4, connectors), oldest minute first")
    soc: list[list[float]] = Field(description="(4, connectors), 0-1")
    connected: list[list[bool]] = Field(description="(4, connectors): minute belongs to the connector's current transaction")
    meter_kw: list[float] = Field(min_length=4, max_length=4, description="site revenue meter, same 4 minutes")


def create_app() -> FastAPI:
    app = FastAPI(title="EV charging telemetry check", version=__version__)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/v1/site-check")
    def site_check(w: SiteWindow) -> dict[str, object]:
        p, s, c = np.asarray(w.power_kw, float), np.asarray(w.soc, float), np.asarray(w.connected, bool)
        if not (p.shape == s.shape == c.shape) or p.ndim != 2 or p.shape[0] != 4:
            raise HTTPException(422, "power_kw, soc and connected must all be (4, connectors)")
        if (s < 0).any() or (s > 1).any() or (p < 0).any():
            raise HTTPException(422, "soc must be within 0-1 and power non-negative")
        bad = np.flatnonzero(implausible_soc(p, s, c))
        meter = np.asarray(w.meter_kw, float)[1:, None]
        mismatch = bool(meter_mismatch(meter, p[1:].sum(1, keepdims=True))[0])
        return {"implausible_connectors": bad.tolist(), "meter_mismatch": mismatch,
                "reported_kw": round(float(p[-1].sum()), 1), "metered_kw": round(float(w.meter_kw[-1]), 1)}

    return app
