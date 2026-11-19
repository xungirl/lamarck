"""Gateway overhead microbenchmark: HTTP round trips through the real gateway to a fake model.

Measures per-request latency (p50/p95/p99) and throughput at several client concurrencies, and
checks that the ledger counted exactly the requests made. No network, no real model.
"""

from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lamarck.gateway.client import GatewayClient  # noqa: E402
from lamarck.gateway.ledger import Ledger  # noqa: E402
from lamarck.gateway.providers import FakeProvider, Router  # noqa: E402
from lamarck.gateway.server import Gateway, GatewayConfig, Grant, start_in_thread  # noqa: E402


def main() -> None:
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        gw = Gateway(Router(None, FakeProvider({"fake-echo": lambda b: "ok"})), Ledger(Path(tmp) / "l.json"),
                     Path(tmp), GatewayConfig(default_rate=1e6))
        server, url = start_in_thread(gw)
        tok = gw.create_grant(Grant("bench", ["policy"], ["*"], rate=1e6))
        client = GatewayClient(url, tok)
        msgs = [{"role": "user", "content": "x" * 2000}]
        total = 0
        for conc in (1, 4, 16):
            n = 400
            lat: list[float] = []

            def one(_: int) -> None:
                t0 = time.perf_counter()
                client.chat("policy", "fake-echo", msgs)
                lat.append((time.perf_counter() - t0) * 1000)

            t0 = time.perf_counter()
            with ThreadPoolExecutor(conc) as pool:
                list(pool.map(one, range(n)))
            wall = time.perf_counter() - t0
            total += n
            q = statistics.quantiles(lat, n=100)
            out[f"concurrency_{conc}"] = {"requests": n, "rps": round(n / wall, 1), "p50_ms": round(q[49], 2),
                                          "p95_ms": round(q[94], 2), "p99_ms": round(q[98], 2)}
        counted = gw.ledger.used("policy")
        out["ledger_counted"] = counted
        out["ledger_exact"] = counted == total
        server.shutdown()
    out["note"] = "in-process gateway, fake model, 2 kB prompt, ledger + audit log written on every request; Apple M2"
    Path("results").mkdir(exist_ok=True)
    Path("results/gateway_bench.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
