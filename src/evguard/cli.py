"""Command line: ``evguard study`` (experiment) and ``evguard serve`` (telemetry check API)."""

from __future__ import annotations

import argparse
import json


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evguard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("study", help="benign training days, attack days with and without the guard, feeder impact")
    st.add_argument("--sessions", required=True, help="Dataset1_charging_reports.csv (Zenodo 13896176)")
    st.add_argument("--out", default="results/feeder")
    st.add_argument("--seed", type=int, default=0)
    sv = sub.add_parser("serve", help="run the API")
    sv.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)
    if a.cmd == "study":
        from .study import run

        s = run(a.sessions, a.out, seed=a.seed)
        print(json.dumps({"false_alarms": s["false_alarms"], "false_mitigations": s["false_mitigations"]}, indent=2))
        return 0
    import uvicorn

    uvicorn.run("evguard.api.app:create_app", factory=True, host="0.0.0.0", port=a.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
