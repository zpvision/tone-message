import asyncio
import json
import math
import sqlite3
import subprocess
import time
import uuid

import httpx

from app.cabinet_store import ACTIVE
from app.cabinet_xlsx import export_result, prepare_rows
from app.context_budget import ContextBudget
from app.load_control import LoadControl


def metrics(store, job_id, timing=None):
    job = store.job(job_id)
    if not job:
        return {}
    timing = timing or job["timing"]
    counts = {row["status"]: row["n"] for row in store.all("SELECT status,count(*) n FROM rows WHERE job_id=? GROUP BY status", (job_id,))}
    values = store.all("SELECT duration,attempts,json_extract(result,'$.prompt_tokens') pt,json_extract(result,'$.output_tokens') ot FROM rows WHERE job_id=? AND status NOT IN ('pending','processing','skipped')", (job_id,))
    durations = sorted(row["duration"] for row in values if row["attempts"])
    done = sum(n for status, n in counts.items() if status not in ("pending", "processing"))
    total = sum(counts.values())
    model_wall = timing.get("model_seconds", 0)
    finished_requests = len(durations)
    rate = finished_requests / model_wall * 60 if model_wall > 0 else 0
    tokens = {"prompt_tokens": 0, "output_tokens": 0}
    for row in values:
        tokens["prompt_tokens"] += row["pt"] or 0
        tokens["output_tokens"] += row["ot"] or 0
    return {"total": total, "processed": done, "success": counts.get("ok", 0),
            "errors": done - counts.get("ok", 0) - counts.get("skipped", 0),
            "skipped": counts.get("skipped", 0), "pending": counts.get("pending", 0),
            "counts": counts, "messages_per_minute": rate,
            "eta_seconds": (total - done) / (rate / 60) if rate else None,
            "mean_request_ms": sum(durations) / len(durations) * 1000 if durations else 0,
            "p50_request_ms": durations[max(0, math.ceil(len(durations) * .5) - 1)] * 1000 if durations else 0,
            "p95_request_ms": durations[max(0, math.ceil(len(durations) * .95) - 1)] * 1000 if durations else 0,
            "attempts": store.one("SELECT coalesce(sum(attempts),0) n FROM rows WHERE job_id=?", (job_id,))["n"],
            "incomplete_attempts": store.one("SELECT count(*) n FROM attempts WHERE job_id=? AND status='interrupted'", (job_id,))["n"],
            **tokens, **timing}


class Engine:
    def __init__(self, store, settings, load_control=None):
        self.store, self.settings = store, settings
        self.task = None
        self.current = None
        self.stop_event = asyncio.Event()
        self.lock = asyncio.Lock()
        self.timing = {}
        self.phase = None
        self.phase_start = self.total_start = 0
        self.base_total = 0
        self.context_budget = ContextBudget(settings)
        from app.inference import Inference, GpuGate
        self.inference = Inference(settings, GpuGate(settings))
        self.load_control = load_control or LoadControl()

    def live_timing(self):
        current = dict(self.timing)
        if self.current:
            elapsed = time.monotonic() - self.phase_start
            if self.phase:
                current[self.phase] = current.get(self.phase, 0) + elapsed
            current["total_seconds"] = self.base_total + time.monotonic() - self.total_start
        return current

    def persist_timing(self):
        if self.current:
            self.store.set_job(self.current, timing=self.live_timing())

    def change_phase(self, phase):
        if self.phase:
            self.timing[self.phase] = self.timing.get(self.phase, 0) + time.monotonic() - self.phase_start
        self.phase, self.phase_start = phase, time.monotonic()
        self.persist_timing()

    async def describe_model(self):
        async with httpx.AsyncClient(base_url=self.settings.inference_url,
                timeout=self.settings.readiness_timeout_seconds, trust_env=False) as client:
            return await self.inference.describe(client)

    async def start(self, request, upload):
        async with self.lock:
            if self.store.active() or (self.task and not self.task.done()):
                raise ValueError("Уже есть активное задание. Остановите его или дождитесь завершения")
            model = await self.describe_model()
            if self.load_control.api_pending:
                raise ValueError("API обрабатывает пакет или ожидает очередь; повторите запуск позже")
            snapshot = {"selection": {**request.model_dump(exclude={"prompts", "parameters"}),
                                      "filename": upload["name"], "file_sha256": json.loads(upload["meta"])["sha256"]},
                        "prompts": request.prompts.model_dump(), "parameters": request.parameters.model_dump(),
                        "model": model, "ollama_num_parallel": self.settings.ollama_num_parallel,
                        "keep_alive": self.settings.keep_alive, "policy": {"network_retries": self.settings.inference_retries, "long_text": "error_no_truncation",
                                                          "context_budget": self.context_budget.metadata(model)}}
            job_id = uuid.uuid4().hex
            self.store.add_job(job_id, snapshot)
            self.launch(job_id)
            return job_id

    def launch(self, job_id):
        self.stop_event = asyncio.Event()
        self.current = job_id
        self.timing = dict(self.store.job(job_id)["timing"])
        self.base_total = self.timing.get("total_seconds", 0)
        self.total_start = self.phase_start = time.monotonic()
        self.phase = None
        self.task = asyncio.create_task(self.run(job_id))

    async def resume(self, job_id):
        async with self.lock:
            if self.store.active() or (self.task and not self.task.done()):
                raise ValueError("Есть активное задание")
            job = self.store.job(job_id)
            if not job or job["status"] not in ("interrupted", "stopped", "failed"):
                raise ValueError("Это задание нельзя продолжить")
            model = await self.describe_model()
            if model["digest"] != job["snapshot"]["model"]["digest"]:
                raise ValueError("Digest модели изменился. Создайте новый запуск")
            if self.load_control.api_pending:
                raise ValueError("API обрабатывает пакет или ожидает очередь")
            self.store.set_job(job_id, status="preparing", error="", export_ready=0)
            self.launch(job_id)

    def stop(self, job_id):
        if self.current != job_id or not self.task or self.task.done():
            raise ValueError("Задание уже не выполняется")
        self.stop_event.set()
        self.store.set_job(job_id, status="stopping")

    async def close(self):
        if self.task and not self.task.done():
            self.persist_timing()
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.store.recover()
        self.context_budget.close()

    async def sample(self, job_id):
        while True:
            try:
                data = await asyncio.to_thread(self.resources)
                self.store.execute("INSERT INTO samples(job_id,elapsed,data) VALUES(?,?,?)",
                                   (job_id, self.live_timing().get("total_seconds", 0), json.dumps(data)))
            except Exception:
                # Sampling failures never expose data or stop a classification job.
                pass
            self.persist_timing()
            await asyncio.sleep(3)

    @staticmethod
    def resources():
        from pathlib import Path
        data = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in ("MemTotal", "MemAvailable"):
                data[key + "_mib"] = int(value.split()[0]) / 1024
        result = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            values = [float(value.strip()) for value in result.stdout.strip().split(",")]
            data.update(zip(("gpu_utilization", "vram_mib", "vram_total_mib"), values))
        return data

    async def run(self, job_id):
        sampler = asyncio.create_task(self.sample(job_id))
        final = "failed"
        try:
            snapshot = self.store.job(job_id)["snapshot"]
            self.change_phase("prepare_seconds")
            await asyncio.to_thread(prepare_rows, self.store, job_id, snapshot["selection"])
            params = snapshot["parameters"]
            async with httpx.AsyncClient(base_url=self.settings.inference_url, trust_env=False,
                                         timeout=params["timeout"], limits=httpx.Limits(max_connections=4)) as client:
                try:
                    async with self.load_control.gpu:
                        await self.context_budget.verify(client, snapshot["model"], params["num_ctx"])
                except Exception:
                    self.store.set_job(job_id, error="context_tokenizer_unavailable_or_not_validated: проверка контекста недоступна; генерация не запускалась")
                    raise
                if not self.stop_event.is_set():
                    self.store.set_job(job_id, status="warming")
                    self.change_phase("warmup_seconds")
                    async with asyncio.timeout(max(300, params["timeout"])):
                        async with self.load_control.gpu:
                            response = await self.inference.chat(client, {"model": self.settings.model_id, "stream": False,
                                "think": False, "keep_alive": self.settings.keep_alive, "messages": [{"role": "user", "content": "Ответь одним словом: готово"}],
                                "options": {"num_ctx": params["num_ctx"], "num_predict": 16, "temperature": 0}}, max(300, params["timeout"]))
                        response.raise_for_status()
                        if not response.json().get("done"):
                            raise ValueError("warmup_incomplete")
                    self.store.set_job(job_id, status="running")
                    self.change_phase("model_seconds")
                    async with asyncio.TaskGroup() as group:
                        for _ in range(params["concurrency"]):
                            group.create_task(self.worker(job_id, snapshot, client))
            final = "stopped" if self.stop_event.is_set() else "completed"
        except asyncio.CancelledError:
            self.persist_timing()
            self.store.set_job(job_id, status="interrupted", error="executor_shutdown")
            raise
        except Exception:
            # No exception text: request bodies, prompts and file content stay out of logs.
            if not self.store.job(job_id)["error"]:
                self.store.set_job(job_id, error="job_failed_check_inference_or_workbook")
            self.store.execute("UPDATE rows SET status='pending' WHERE job_id=? AND status='processing'", (job_id,))
            self.store.execute("UPDATE attempts SET status='interrupted',error='duration_incomplete' WHERE job_id=? AND status='running'", (job_id,))
        finally:
            sampler.cancel()
            try:
                await sampler
            except asyncio.CancelledError:
                pass
        try:
            self.store.set_job(job_id, status="exporting")
            self.change_phase("export_seconds")
            await asyncio.to_thread(export_result, self.store, job_id, metrics(self.store, job_id, self.live_timing()))
            self.change_phase(None)
            self.store.set_job(job_id, status=final, export_ready=1, timing=self.live_timing())
        except asyncio.CancelledError:
            self.store.set_job(job_id, status="interrupted", error="export_interrupted", timing=self.live_timing())
            raise
        except Exception:
            self.store.set_job(job_id, status="failed", error="export_failed", timing=self.live_timing())
        finally:
            self.current = None

    async def worker(self, job_id, snapshot, client):
        params = snapshot["parameters"]
        while not self.stop_event.is_set():
            row = self.store.claim(job_id)
            if not row:
                return
            number = row["row_num"]
            prompt = snapshot["prompts"]["template"].replace("{text}", row["text"])
            system = snapshot["prompts"]["system"]
            budget = self.context_budget.count(system, prompt, params)
            if budget["remaining_tokens"] < 0:
                self.store.finish_row(job_id, number, "input_error", {"context_budget": budget,
                    "error": f'context_budget_exceeded: вход {budget["input_tokens"]} + ответ {budget["output_reserve"]} = '
                             f'{budget["required_tokens"]} токенов > контекст {budget["context_tokens"]}; вход не обрезан'})
                continue
            payload = {"model": self.settings.model_id, "stream": False, "think": params["thinking"], "keep_alive": self.settings.keep_alive,
                       "truncate": False, "shift": False,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                       "options": {key: params[key] for key in ("temperature", "num_ctx", "num_predict")}}
            for retry in range(self.settings.inference_retries + 1):
                if retry and self.stop_event.is_set():
                    self.store.finish_row(job_id, number, status, result)
                    break
                attempt = self.store.begin_attempt(job_id, number)
                start = time.monotonic()
                result, status, error, retryable = {"context_budget": budget}, "inference_error", "inference_error", False
                try:
                    async with asyncio.timeout(params["timeout"]):
                        async with self.load_control.gpu:
                            response = await self.inference.chat(client, payload, params["timeout"])
                        result["http_status"] = response.status_code
                        result["response"] = response.text
                        response.raise_for_status()
                        raw = response.json()
                    content = raw.get("message", {}).get("content", "")
                    result.update({"raw": raw, "response": content if isinstance(content, str) else json.dumps(content),
                                   "thinking": raw.get("message", {}).get("thinking", ""),
                                   "prompt_tokens": raw.get("prompt_eval_count", 0), "output_tokens": raw.get("eval_count", 0),
                                   **{name: raw.get(field, 0) / 1e6 for name, field in
                                      (("load_ms", "load_duration"), ("prompt_eval_ms", "prompt_eval_duration"), ("eval_ms", "eval_duration"))}})
                    try:
                        label = json.loads(content).get("label")
                        if label not in ("negative", "neutral", "positive"):
                            raise ValueError("invalid_label")
                        if not raw.get("done") or raw.get("done_reason") != "stop":
                            status, error = "truncated", "generation_not_finished_naturally"
                        else:
                            result["label"], status, error = label, "ok", ""
                    except (ValueError, TypeError, AttributeError):
                        status, error = "parse_error", "invalid_json_label"
                    if raw.get("prompt_eval_count") != budget["input_tokens"]:
                        # Different counters alone do not establish truncation.
                        # Guard input before sending; Ollama also receives
                        # truncate=false/shift=false. Keep valid JSON successful.
                        actual = raw.get("prompt_eval_count")
                        result["warnings"] = [{"code": "context_token_count_mismatch",
                            "local_input_tokens": budget["input_tokens"], "ollama_input_tokens": actual,
                            "delta": actual - budget["input_tokens"] if isinstance(actual, int) else None,
                            "message": "Счётчики токенов расходятся; это само по себе не доказывает обрезание входа"}]
                except (TimeoutError, httpx.TimeoutException):
                    status, error = "timeout", "request_timeout_no_automatic_retry"
                except (httpx.NetworkError, httpx.RemoteProtocolError):
                    error, retryable = "network_error", True
                except httpx.HTTPStatusError as failure:
                    error, retryable = f"inference_http_{failure.response.status_code}", failure.response.status_code in (502, 503)
                    result["response"] = failure.response.text
                except (ValueError, TypeError, AttributeError):
                    status, error = "parse_error", "invalid_inference_response"
                duration = time.monotonic() - start
                result["error"] = error
                self.store.finish_attempt(job_id, number, attempt, duration, status, error)
                if retryable and retry < self.settings.inference_retries and not self.stop_event.is_set():
                    await asyncio.sleep(self.settings.inference_retry_delay)
                    continue
                self.store.finish_row(job_id, number, status, result)
                self.persist_timing()
                break
