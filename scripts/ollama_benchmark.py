"""Sequential Russian generation smoke test, not a sentiment quality benchmark."""
import os
import json
from pathlib import Path
import subprocess
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:11434"
MODEL = os.environ["FINKOPER_MODEL_ID"]
OUT = Path("/opt/finkoper-ai/verification/ollama-benchmark.json")


def api(path, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(BASE + path, data=data,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def snapshot():
    fields = ["memory.used", "memory.free", "utilization.gpu", "utilization.memory", "power.draw"]
    values = subprocess.check_output(["nvidia-smi", "--query-gpu=" + ",".join(fields),
                                      "--format=csv,noheader,nounits"], text=True).strip().split(",")
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in ("MemTotal", "MemAvailable", "MemFree"):
            info[key + "_mib"] = int(value.split()[0]) / 1024
    rss = subprocess.check_output(["ps", "-C", "ollama", "-o", "rss="], text=True)
    return {"monotonic": time.monotonic(),
            **{key: float(value.strip()) for key, value in zip(fields, values)},
            **info, "ollama_daemon_rss_mib": sum(int(v) for v in rss.split()) / 1024}


def main():
    # Make the first request truly cold with respect to runner residency. OS
    # page cache is intentionally not dropped on this shared server.
    api("/api/generate", {"model": MODEL, "keep_alive": 0})
    for _ in range(100):
        if not api("/api/ps")["models"]:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("Runner did not unload before the cold request")
    report = {"version": api("/api/version"), "tags": api("/api/tags"),
              "show": api("/api/show", {"model": MODEL}),
              "cold_definition": "runner unloaded; operating system page cache not dropped",
              "before": snapshot(), "requests": []}
    prompts = [
        "Ответь одним коротким предложением по-русски: как называется столица Франции?",
        "Коротко перефразируй по-русски, сохрани смысл и верни только одну фразу: Спасибо, всё отлично!",
        "Коротко перефразируй по-русски, сохрани смысл и верни только одну фразу: Когда будет готов отчёт?",
        "Коротко перефразируй по-русски, сохрани смысл и верни только одну фразу: Мне на сбер 😂",
        "Коротко перефразируй по-русски, сохрани смысл и верни только одну фразу: Вы опять пропустили срок, я недоволен",
    ]
    for index, prompt in enumerate(prompts):
        payload = {"model": MODEL, "prompt": prompt, "stream": False,
                   "think": False, "keep_alive": "30m",
                   "options": {"num_ctx": 4096, "temperature": 0, "num_predict": 128}}
        samples, errors = [], []
        stop = threading.Event()

        def sample():
            while not stop.is_set():
                try:
                    samples.append(snapshot())
                except Exception as error:
                    errors.append(type(error).__name__)
                stop.wait(0.2)

        worker = threading.Thread(target=sample, daemon=True)
        worker.start()
        start = time.perf_counter()
        try:
            response = api("/api/generate", payload)
            elapsed = time.perf_counter() - start
        finally:
            stop.set()
            worker.join(timeout=5)
        entry = {"kind": "cold" if index == 0 else "warm", "payload": payload,
                 "response": response, "wall_seconds": elapsed,
                 "gpu_ram_samples": samples, "sampling_errors": errors,
                 "after": snapshot(), "resident": api("/api/ps")}
        report["requests"].append(entry)
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"kind": entry["kind"], "wall_seconds": elapsed,
                          "response": response}, ensure_ascii=False), flush=True)
        if not response.get("done") or response.get("done_reason") != "stop":
            raise RuntimeError("Generation did not stop naturally; inspect the saved response")
        text = response.get("response", "")
        if not text.strip() or not any("А" <= char <= "я" for char in text):
            raise RuntimeError("No Russian text generated")
        if index == 0 and "париж" not in text.lower():
            raise RuntimeError("Cold response did not answer the factual smoke question")
        models = entry["resident"]["models"]
        matching = [model for model in models if model.get("name") == MODEL]
        if len(matching) != 1 or matching[0].get("context_length") != 4096:
            raise RuntimeError("Unexpected loaded model or context length")
    report["after"] = snapshot()
    report["ollama_ps"] = subprocess.check_output(["ollama", "ps"], text=True)
    if "100% GPU" not in report["ollama_ps"]:
        raise RuntimeError("ollama ps does not confirm 100% GPU")
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(report["ollama_ps"], flush=True)


if __name__ == "__main__":
    main()
