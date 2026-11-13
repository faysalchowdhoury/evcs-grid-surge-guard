"""Distribution feeder impact with pandapower.

The CIGRE 20-kV medium-voltage benchmark network (two feeders from a 110/20-kV substation) with its
loads scaled by a typical daily shape (45% at night to 82% in the evening peak). Three fast-charging
plazas connect near feeder ends (buses 5, 10 and 14), where they stress voltage and line loading most.
Each minute's plaza power from the charging simulation is applied at 0.98 power factor and an AC power
flow is solved.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import pandapower as pp
    import pandapower.networks as pn

PLAZA_BUSES = (5, 10, 14)
HOURLY = np.array([0.50, 0.47, 0.45, 0.45, 0.46, 0.50, 0.58, 0.66, 0.70, 0.70, 0.70, 0.70,
                   0.70, 0.69, 0.68, 0.68, 0.72, 0.78, 0.82, 0.80, 0.75, 0.68, 0.60, 0.54])
PF = 0.98


@dataclass
class Feeder:
    net: object
    base_p: np.ndarray
    base_q: np.ndarray
    plaza_loads: list[int]

    @classmethod
    def build(cls) -> "Feeder":
        net = pn.create_cigre_network_mv(with_der=False)
        base_p, base_q = net.load["p_mw"].to_numpy().copy(), net.load["q_mvar"].to_numpy().copy()
        idx = [int(pp.create_load(net, bus=b, p_mw=0.0, q_mvar=0.0, name=f"DCFC plaza {k + 1}")) for k, b in enumerate(PLAZA_BUSES)]
        return cls(net, base_p, base_q, idx)

    def solve(self, minute: int, plaza_kw: np.ndarray) -> dict[str, float]:
        f = HOURLY[(minute // 60) % 24]
        n = len(self.base_p)
        self.net.load.loc[: n - 1, "p_mw"] = self.base_p * f
        self.net.load.loc[: n - 1, "q_mvar"] = self.base_q * f
        p = plaza_kw / 1000.0
        self.net.load.loc[self.plaza_loads, "p_mw"] = p
        self.net.load.loc[self.plaza_loads, "q_mvar"] = p * np.tan(np.arccos(PF))
        pp.runpp(self.net, numba=False)
        return {"line_max_pct": float(self.net.res_line["loading_percent"].max()),
                "trafo_max_pct": float(self.net.res_trafo["loading_percent"].max()),
                "v_min_pu": float(self.net.res_bus["vm_pu"].min())}

    def series(self, minutes: np.ndarray, plaza_kw: np.ndarray) -> dict[str, np.ndarray]:
        rows = [self.solve(int(m), plaza_kw[i]) for i, m in enumerate(minutes)]
        return {k: np.array([r[k] for r in rows]) for k in rows[0]}
