"""Verify real unloaded readiness, then restore the model with one GPU request."""
import os
import json
from pathlib import Path
import time
import urllib.request


def request(port, path, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    with urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/json"}
    ), timeout=300) as response:
        return response.status, json.load(response)


request(11434, "/api/generate", {"model": os.environ["FINKOPER_MODEL_ID"], "keep_alive": 0})
for _ in range(100):
    if not request(11434, "/api/ps")[1]["models"]:
        break
    time.sleep(0.1)
else:
    raise RuntimeError("Model was not unloaded")
code, unloaded = request(8000, "/readyz")
assert code == 200 and unloaded["model"]["installed"] and unloaded["model"]["loaded"] is False, unloaded
payload = {"model": os.environ["FINKOPER_MODEL_ID"], "prompt": "Ответь одним коротким предложением по-русски: как называется столица Франции?",
           "stream": False, "think": False, "keep_alive": "30m",
           "options": {"num_ctx": 4096, "temperature": 0, "num_predict": 128}}
start = time.perf_counter()
_, response = request(11434, "/api/generate", payload)
elapsed = time.perf_counter() - start
assert response["done_reason"] == "stop" and "париж" in response["response"].lower(), response
code, reloaded = request(8000, "/readyz")
assert code == 200 and reloaded["model"]["loaded"] is True, reloaded
result = {"unloaded_readyz": unloaded, "reload_wall_seconds": elapsed,
          "payload": payload, "response": response, "reloaded_readyz": reloaded}
Path("/opt/finkoper-ai/verification/ollama-residency.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(result, ensure_ascii=False, indent=2))
