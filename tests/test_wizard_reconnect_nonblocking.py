"""``POST /v1/wizard/reconnect`` must not hold the API's event loop (T3.10)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from nvh.integrations.wizard import reconnect as reconnect_module


def test_a_slow_reconnect_does_not_block_the_event_loop(monkeypatch) -> None:
    from nvh.api.server import app

    inside_probe = threading.Event()

    def slow_preflight(home_dir=None) -> dict:
        inside_probe.set()
        time.sleep(1.0)
        return {"first_run": False, "changed": False, "changes": [], "fingerprint": {}}

    monkeypatch.setattr(reconnect_module, "run_boot_preflight", slow_preflight)
    with TestClient(app, raise_server_exceptions=True) as shared:
        with ThreadPoolExecutor(max_workers=1) as pool:
            slow = pool.submit(shared.post, "/v1/wizard/reconnect", json={})
            assert inside_probe.wait(timeout=10), "the reconnect never reached the probe"
            started = time.perf_counter()
            health = shared.get("/v1/health")
            elapsed = time.perf_counter() - started
            resp = slow.result(timeout=15)
    print(f"health latency during reconnect: {elapsed * 1000:.0f} ms")
    assert health.status_code == 200 and elapsed < 0.6, f"health waited {elapsed:.2f}s behind the reconnect"
    assert resp.status_code == 200 and "summary" in resp.json()["data"]
