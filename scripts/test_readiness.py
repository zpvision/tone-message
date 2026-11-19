"""Readiness failure/recovery tests. No production settings or services changed."""
import asyncio
import socket
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.readiness import check_readiness


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_states_and_recovery(self):
        state = {"available": True, "installed": True, "loaded": False}
        paths = []

        async def upstream(request):
            paths.append(request.url.path)
            if not state["available"]:
                raise httpx.ConnectError("test upstream offline", request=request)
            key = "installed" if request.url.path == "/api/tags" else "loaded"
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}] if state[key] else []})

        settings = Settings(inference_url="http://127.0.0.1:11434", model_id="qwen3:8b")
        async with httpx.AsyncClient(base_url=settings.inference_url,
                                    transport=httpx.MockTransport(upstream)) as client:
            code, body = await check_readiness(settings, client)
            self.assertEqual(code, 200)
            self.assertFalse(body["model"]["loaded"])
            state["available"] = False
            code, body = await check_readiness(settings, client)
            self.assertEqual((code, body["reason"]), (503, "inference_unavailable"))
            self.assertFalse(body["inference"]["available"])
            self.assertIsNone(body["model"]["installed"])
            state["available"] = True
            state["installed"] = False
            code, body = await check_readiness(settings, client)
            self.assertEqual((code, body["reason"]), (503, "model_not_installed"))
            state["installed"] = state["loaded"] = True
            code, body = await check_readiness(settings, client)
            self.assertEqual(code, 200)
            self.assertTrue(body["model"]["loaded"])
        self.assertLessEqual(set(paths), {"/api/tags", "/api/ps"})

    async def test_optional_residency_timeout(self):
        async def upstream(request):
            if request.url.path == "/api/ps":
                await asyncio.sleep(1)
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})

        settings = Settings(inference_url="http://127.0.0.1:11434", model_id="qwen3:8b",
                            readiness_timeout_seconds=0.1)
        async with httpx.AsyncClient(base_url=settings.inference_url,
                                    transport=httpx.MockTransport(upstream)) as client:
            code, body = await check_readiness(settings, client)
        self.assertEqual(code, 200)
        self.assertIsNone(body["model"]["loaded"])

    async def test_bad_response_and_timeout(self):
        settings = Settings(inference_url="http://127.0.0.1:11434", model_id="qwen3:8b",
                            readiness_timeout_seconds=0.1)
        async def invalid(request):
            return httpx.Response(200, json={"unexpected": "format"})

        async def slow(request):
            await asyncio.sleep(1)

        for handler, reason in ((invalid, "inference_check_failed"), (slow, "inference_unavailable")):
            async with httpx.AsyncClient(base_url=settings.inference_url,
                                        transport=httpx.MockTransport(handler)) as client:
                code, body = await check_readiness(settings, client)
                self.assertEqual((code, body["reason"]), (503, reason))

    def test_real_unavailable_upstream_health_is_independent(self):
        # Reserve an unused port without listen(): connections are really refused.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
            settings = Settings(inference_url=f"http://127.0.0.1:{port}", model_id="qwen3:8b",
                                readiness_timeout_seconds=0.5)
            with TestClient(create_app(settings)) as client:
                self.assertEqual(client.get("/healthz").status_code, 200)
                response = client.get("/readyz")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["reason"], "inference_unavailable")
                self.assertEqual(client.get("/healthz").status_code, 200)

    def test_real_http_recovery_via_disposable_loopback_upstream(self):
        # Only /tags and /ps are relayed to the real Ollama instance. This
        # temporary listener never affects production configuration or service.
        class Relay(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ("/api/tags", "/api/ps"):
                    self.send_error(404)
                    return
                with urllib.request.urlopen("http://127.0.0.1:11434" + self.path, timeout=2) as response:
                    payload = response.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            address = reserved.getsockname()
            settings = Settings(inference_url=f"http://127.0.0.1:{address[1]}",
                                model_id="qwen3:8b", readiness_timeout_seconds=2)
            with TestClient(create_app(settings)) as client:
                offline = client.get("/readyz")
                self.assertEqual(offline.status_code, 503)
                self.assertEqual(offline.json()["reason"], "inference_unavailable")
                server = ThreadingHTTPServer(address, Relay, bind_and_activate=False)
                server.socket.close()
                server.socket = reserved
                server.server_activate()
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    restored = client.get("/readyz")
                    self.assertEqual(restored.status_code, 200, restored.text)
                    self.assertTrue(restored.json()["model"]["installed"])
                    self.assertEqual(client.get("/healthz").status_code, 200)
                    print("Real HTTP upstream: 503 offline -> 200 recovered; healthz=200")
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(timeout=3)
                response = client.get("/readyz")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["reason"], "inference_unavailable")
                self.assertEqual(client.get("/healthz").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
