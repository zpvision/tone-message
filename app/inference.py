"""Shared model-neutral Ollama adapter. Profiles reuse one configured model."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import fcntl
import logging
from pathlib import Path
import time
import httpx


class ModelMismatch(ValueError):
    pass


@dataclass(frozen=True)
class InferenceProfile:
    name: str
    num_ctx: int
    max_output: int
    temperature: float = 0
    thinking: bool = False
    timeout: float = 180
    workers: int = 1


class GpuGate:
    """Bounded in-process + cooperative cross-process GPU slots.
    Other functions must use the same lock directory and capacity.
    Cancellation always releases both permits; waits are finite.
    """
    def __init__(self, settings):
        self.settings = settings
        self.local = asyncio.Semaphore(settings.inference_concurrency)

    async def __aenter__(self):
        raise RuntimeError("use gate.slot() so ownership is task-local")

    @asynccontextmanager
    async def slot(self):
        fd = None
        acquired_local = False
        try:
            # One budget covers both queues; inference itself is outside it.
            async with asyncio.timeout(self.settings.inference_lock_timeout):
                await self.local.acquire()
                acquired_local = True
                root = Path(self.settings.inference_lock_dir)
                root.mkdir(parents=True, exist_ok=True, mode=0o770)
                while fd is None:
                    for index in range(self.settings.inference_concurrency):
                        handle = (root / (str(index) + ".lock")).open("a")
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fd = handle
                            break
                        except BlockingIOError:
                            handle.close()
                        except BaseException:
                            handle.close()
                            raise
                    if fd is None:
                        await asyncio.sleep(.025)
            yield
        finally:
            if fd:
                fcntl.flock(fd, fcntl.LOCK_UN)
                fd.close()
            if acquired_local:
                self.local.release()



class Inference:
    def __init__(self, settings, gate):
        self.settings, self.gate = settings, gate

    async def describe(self, client):
        r = await client.get("/api/tags")
        r.raise_for_status()
        model = next((m for m in r.json()["models"] if
                      m.get("name") == self.settings.model_id), None)
        if model is None:
            logging.getLogger("uvicorn.error").error("configured_model_not_installed model=%s", self.settings.model_id)
            raise ValueError("configured_model_not_installed: " + self.settings.model_id)
        if self.settings.model_digest and model["digest"] != self.settings.model_digest:
            logging.getLogger("uvicorn.error").error("configured_model_digest_mismatch model=%s", self.settings.model_id)
            raise ValueError("configured_model_digest_mismatch")
        version = await client.get("/api/version")
        version.raise_for_status()
        return {"name": self.settings.model_id, "digest": model["digest"],
                "size": model["size"], "quantization": model.get("details", {}).get("quantization_level"),
                "ollama_version": version.json().get("version")}

    def payload(self, profile, messages, schema=None):
        result = {"model": self.settings.model_id, "stream": False,
                  "think": profile.thinking, "keep_alive": self.settings.keep_alive,
                  "truncate": False, "shift": False, "messages": messages,
                  "options": {"temperature": profile.temperature, "num_ctx": profile.num_ctx,
                              "num_predict": profile.max_output}}
        if schema is not None:
            result["format"] = schema
        return result

    async def chat(self, client, payload, timeout, stats=None):
        async with asyncio.timeout(timeout):
            queued = time.monotonic()
            async with self.gate.slot():
                acquired = time.monotonic()
                response = await client.post("/api/chat", json=payload, timeout=timeout)
                finished = time.monotonic()
        if response.is_success:
            try:
                returned = response.json().get("model")
            except (ValueError, TypeError, AttributeError):
                returned = None
            if returned != self.settings.model_id:
                logging.getLogger("uvicorn.error").error("inference_response_model_mismatch configured=%s",
                    self.settings.model_id)
                raise ModelMismatch("inference_response_model_mismatch")
        if stats is not None:
            stats.setdefault("gpu_wait_seconds", []).append(acquired-queued)
            stats.setdefault("inference_seconds", []).append(finished-acquired)
            try:
                raw = response.json()
                for name in ("load_duration", "prompt_eval_duration", "eval_duration"):
                    stats[name] = stats.get(name, 0) + raw.get(name, 0)/1e9
            except (TypeError, KeyError):
                pass
        return response

    def log_effective(self):
        s = self.settings
        logging.getLogger("uvicorn.error").info(
            "effective_inference backend=%s url=%s model=%s digest=%s profile=%s keep_alive=%s "
            "gpu_slots=%s workers=%s queue_batches=%s queue_messages=%s queue_timeout=%s retries=%s",
            s.inference_backend, s.inference_url, s.model_id, s.model_digest or "unpinned",
            s.profile(), s.inference_keep_alive, s.inference_concurrency, s.sentiment_workers,
            s.api_max_waiting_batches, s.api_max_waiting_messages, s.api_queue_timeout,
            s.inference_retries)
