"""Small real-GPU end-to-end acceptance on the deployed loopback backend."""
import io
import json
from pathlib import Path
import subprocess
import time

import httpx
from openpyxl import Workbook, load_workbook

BASE = "http://127.0.0.1:8000"
HEADERS = {"X-Cabinet-Request": "1"}
REPORT = {}


def call(method, path, **kwargs):
    with httpx.Client(base_url=BASE, headers=HEADERS, timeout=30, trust_env=False) as client:
        result = client.request(method, path, **kwargs)
        result.raise_for_status()
        return result.json()


def wait(job_id, states=("completed", "failed", "stopped", "interrupted"), timeout=300):
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        value = call("GET", f"/api/jobs/{job_id}")
        seen.append([value["status"], value["metrics"]["processed"]])
        if value["status"] in states:
            return value, seen
        time.sleep(.1)
    raise RuntimeError("Acceptance job timed out")


def file_bytes(rows=8):
    book = Workbook()
    sheet = book.active
    sheet.title = "Сообщения"
    sheet.append(["ID", "Текст", "ai_label"])
    phrases = ["Спасибо, всё отлично!", "Когда будет готов отчёт?", "Мне на сбер 😂", "Вы опять пропустили срок, я недоволен", None, "=1+1", "я" * 6000, "Нужны документы"]
    for i in range(rows):
        sheet.append([i + 1, phrases[i % len(phrases)], "original"])
    book.create_sheet("Справочник").append(["Оставить без изменений", 123])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def upload(data):
    return call("POST", "/api/uploads", files={"file": ("Синтетический тест.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})


def start(upload_id, prompts, limit=None, concurrency=1):
    return call("POST", "/api/jobs", json={"upload_id": upload_id, "sheet": "Сообщения", "column": 2,
                "headers": True, "limit": limit, "prompts": prompts,
                "parameters": {"temperature": 0, "num_ctx": 4096, "num_predict": 128,
                               "thinking": False, "concurrency": concurrency, "timeout": 180}})["id"]


def main():
    config = call("GET", "/api/config")
    default_prompts = config["prompts"]
    saved = call("POST", "/api/prompts", json=default_prompts)
    assert call("GET", "/api/config")["prompts"] == default_prompts
    assert any(p["id"] == saved["id"] for p in call("GET", "/api/prompts"))
    data = file_bytes()
    source = load_workbook(io.BytesIO(data), data_only=False)
    upload_info = upload(data)
    preview = call("GET", f'/api/uploads/{upload_info["id"]}/preview', params={"sheet": "Сообщения", "headers": True})
    assert preview["total"] == 8 and preview["columns"][1]["index"] == 2
    assert call("GET", f'/api/uploads/{upload_info["id"]}/preview', params={"sheet": "Справочник", "headers": False})["total"] == 1
    job = start(upload_info["id"], default_prompts)
    # Each call closes its HTTP client; job runs entirely outside client lifetime.
    time.sleep(.4)
    altered = {"system": 'Верни только слово: тест', "template": "Текст: {text}; JSON {не_шаблон}"}
    call("POST", "/api/prompts", json=altered)
    finished, progress = wait(job)
    assert finished["status"] == "completed", finished["error"]
    assert finished["snapshot"]["prompts"] == default_prompts
    assert finished["metrics"]["total"] == finished["metrics"]["processed"] == 8
    assert finished["metrics"]["skipped"] == 1 and finished["metrics"]["errors"] >= 2
    with httpx.Client(base_url=BASE, trust_env=False) as client:
        result = client.get(f"/api/jobs/{job}/download/xlsx")
        result.raise_for_status()
        workbook = load_workbook(io.BytesIO(result.content), data_only=False)
        for name in source.sheetnames:
            for row in source[name]:
                for cell in row:
                    assert workbook[name][cell.coordinate].value == cell.value, (name, cell.coordinate)
        assert "Параметры теста" in workbook.sheetnames
        columns = [c.value for c in workbook["Сообщения"][1]]
        assert "ai_label_2" in columns
        for row in range(2, 10):
            assert workbook["Сообщения"].cell(row, columns.index("ai_response") + 1).data_type == "s" or workbook["Сообщения"].cell(row, columns.index("ai_response") + 1).value is None
        workbook.close()
        details = client.get(f"/api/jobs/{job}/download/jsonl")
        details.raise_for_status()
        assert len(details.text.splitlines()) == 8
        model_rows = [json.loads(line) for line in details.text.splitlines() if json.loads(line)["attempts"]]
        assert all(row["result"].get("raw", {}).get("done") for row in model_rows)
    source.close()
    REPORT["main"] = {"id": job, "metrics": finished["metrics"], "progress": progress,
                       "preserved_source_sheets_and_values": True, "snapshot_immutable": True,
                       "closed_and_reopened_client": True, "prompt_saved_reloaded": True}
    parse_job = start(upload_info["id"], altered, limit=1)
    parsed, _ = wait(parse_job)
    assert parsed["metrics"]["counts"].get("parse_error") == 1, parsed["metrics"]
    REPORT["parse_error"] = {"id": parse_job, "metrics": parsed["metrics"]}
    # A tiny controlled batch exercises stop and the exclusive active-job lock.
    more = upload(file_bytes(24))
    stopped_job = start(more["id"], default_prompts, concurrency=4)
    running, _ = wait(stopped_job, states=("running",), timeout=120)
    with httpx.Client(base_url=BASE, headers=HEADERS, trust_env=False) as client:
        conflict = client.post("/api/jobs", json={"upload_id": more["id"], "sheet": "Сообщения", "column": 2})
        assert conflict.status_code == 409
    call("POST", f"/api/jobs/{stopped_job}/stop")
    stopped, _ = wait(stopped_job)
    assert stopped["status"] == "stopped" and stopped["metrics"]["pending"] > 0
    before_attempts = stopped["metrics"]["attempts"]
    call("POST", f"/api/jobs/{stopped_job}/resume")
    continued, _ = wait(stopped_job)
    assert continued["status"] == "completed" and continued["metrics"]["processed"] == 24
    assert continued["metrics"]["attempts"] <= 15  # 15 non-empty/non-formula/short rows, no repeats.
    REPORT["stop_resume"] = {"id": stopped_job, "before": stopped["metrics"], "after": continued["metrics"]}
    # Real backend restart: retain completed rows and mark lost executor honestly.
    restarted_job = start(more["id"], default_prompts, concurrency=1)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        current = call("GET", f"/api/jobs/{restarted_job}")
        if current["status"] == "running" and current["metrics"]["success"] >= 1:
            break
        time.sleep(.05)
    else:
        raise RuntimeError("No running restart target")
    subprocess.run(["sudo", "-n", "systemctl", "restart", "finkoper-ai.service"], check=True)
    for _ in range(200):
        try:
            after = call("GET", f"/api/jobs/{restarted_job}")
            break
        except httpx.HTTPError:
            time.sleep(.1)
    assert after["status"] == "interrupted", after["status"]
    assert after["metrics"]["success"] >= current["metrics"]["success"]
    assert call("POST", f"/api/jobs/{restarted_job}/export")["export_ready"]
    with httpx.Client(base_url=BASE, trust_env=False) as client:
        partial = client.get(f"/api/jobs/{restarted_job}/download/xlsx")
        partial.raise_for_status()
        partial_book = load_workbook(io.BytesIO(partial.content))
        partial_book.close()
    call("POST", f"/api/jobs/{restarted_job}/resume")
    resumed, _ = wait(restarted_job)
    assert resumed["status"] == "completed" and resumed["metrics"]["processed"] == 24
    REPORT["restart_resume"] = {"id": restarted_job, "interrupted": after["metrics"], "after": resumed["metrics"]}
    assert call("GET", f"/api/jobs/{job}")["export_ready"]
    with httpx.Client(base_url=BASE, headers=HEADERS, trust_env=False) as client:
        assert client.delete(f"/api/jobs/{stopped_job}").status_code == 400
    for identifier in (stopped_job, restarted_job):
        assert call("DELETE", f"/api/jobs/{identifier}?confirm=true")["deleted"]
    call("POST", "/api/prompts", json=default_prompts)
    REPORT["deletion_confirmed"] = True
    REPORT["services"] = subprocess.check_output(["systemctl", "is-active", "finkoper-ai", "ollama", "nginx"], text=True)
    REPORT["ollama_ps"] = subprocess.check_output(["ollama", "ps"], text=True)
    assert "100% GPU" in REPORT["ollama_ps"]
    path = Path("/opt/finkoper-ai/verification/cabinet-integration.json")
    path.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(REPORT, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
