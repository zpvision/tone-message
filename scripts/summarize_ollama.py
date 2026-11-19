"""Summarize measured values without changing raw benchmark responses."""
import json
from pathlib import Path

root = Path("/opt/finkoper-ai/verification")
report = json.loads((root / "ollama-benchmark.json").read_text())
rows = []
for request in report["requests"]:
    response = request["response"]
    samples = request["gpu_ram_samples"]
    duration = response.get("eval_duration", 0) / 1e9
    row = {
        "kind": request["kind"], "wall_seconds": request["wall_seconds"],
        "answer": response["response"], "done_reason": response.get("done_reason"),
        "prompt_tokens": response.get("prompt_eval_count"),
        "output_tokens": response.get("eval_count"),
        "total_seconds": response.get("total_duration", 0) / 1e9,
        "load_seconds": response.get("load_duration", 0) / 1e9,
        "prompt_eval_seconds": response.get("prompt_eval_duration", 0) / 1e9,
        "eval_seconds": duration,
        "tokens_per_second": response.get("eval_count", 0) / duration if duration else None,
        "peak_vram_mib": max(s["memory.used"] for s in samples),
        "peak_gpu_utilization_percent": max(s["utilization.gpu"] for s in samples),
        "peak_ram_used_mib": max(s["MemTotal_mib"] - s["MemAvailable_mib"] for s in samples),
        "after_vram_mib": request["after"]["memory.used"],
        "sample_count": len(samples), "sampling_errors": request["sampling_errors"],
        "resident": request["resident"],
    }
    rows.append(row)
summary = {"version": report["version"], "models": report["tags"]["models"],
           "before": report["before"], "after": report["after"],
           "requests": rows, "ollama_ps": report["ollama_ps"]}
(root / "ollama-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(summary, ensure_ascii=False, indent=2))
