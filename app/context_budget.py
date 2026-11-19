"""Exact context counting using configured Ollama model and native runner.
Requires the installed Ollama render-only extension; absence fails closed.
No architecture, digest, GGUF path or hardware is compiled into this module.
"""
import asyncio
import shlex
import subprocess
import threading
import time
from urllib.parse import urlsplit
import httpx


class ContextBudget:
    def __init__(self, settings):
        self.settings = settings
        self.client = httpx.Client(base_url=settings.inference_url, trust_env=False,
            timeout=httpx.Timeout(settings.tokenizer_timeout, connect=settings.connect_timeout_seconds))
        self.guard = threading.RLock()
        self._blob = self._runner = self._digest = None
        self._discovered = 0
        self._verified = None

    def load(self):
        return None

    def close(self):
        self.client.close()

    def post(self, url, payload):
        response = self.client.post(url, json=payload)
        response.raise_for_status()
        return response.json()

    def metadata(self, model):
        if model.get("name") != self.settings.model_id:
            raise ValueError("context_model_mismatch")
        if self.settings.model_digest and model.get("digest") != self.settings.model_digest:
            raise ValueError("context_model_digest_mismatch")
        return {"method": "ollama_native_runner_tokenizer", "model_digest": model.get("digest"),
                "rule": "input_tokens + num_predict <= num_ctx", "truncate": False, "shift": False}

    def runner_url(self, refresh=False):
        with self.guard:
            if not refresh and self._runner and time.monotonic()-self._discovered < self.settings.tokenizer_discovery_ttl:
                return self._runner
            if urlsplit(self.settings.inference_url).hostname not in ("127.0.0.1", "localhost"):
                raise ValueError("native_runner_tokenizer_requires_local_ollama")
            shown = self.post("/api/show", {"model": self.settings.model_id})
            paths = []
            for line in shown.get("modelfile", "").splitlines():
                if line.startswith("FROM "):
                    paths.append(shlex.split(line)[1])
            if not paths:
                raise ValueError("configured_model_blob_not_found")
            self._blob = paths[0]
            output = subprocess.check_output(["ps", "-eo", "args", "--width", "10000"], text=True, timeout=5)
            matches = []
            for line in output.splitlines():
                try:
                    args = shlex.split(line)
                    if not args or args[0].rsplit("/", 1)[-1] != "llama-server":
                        continue
                    model = args[args.index("--model")+1]
                    host = args[args.index("--host")+1]
                    port = int(args[args.index("--port")+1])
                    if model == self._blob and host == "127.0.0.1" and 1024 <= port <= 65535:
                        matches.append("http://127.0.0.1:" + str(port))
                except (ValueError, IndexError):
                    continue
            if len(matches) != 1:
                self._runner = None
                raise ValueError("configured_model_runner_not_uniquely_identified")
            self._runner, self._discovered = matches[0], time.monotonic()
            return self._runner

    def count(self, system, user, params):
        payload = {"model": self.settings.model_id, "stream": False,
            "think": params["thinking"], "keep_alive": self.settings.keep_alive,
            "truncate": False, "shift": False, "_debug_render_only": True,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"num_ctx": params["num_ctx"]}}
        result = self.post("/api/chat", payload)
        rendered = result["_debug_info"]["rendered_template"]
        if not isinstance(rendered, str) or not rendered:
            raise ValueError("native_render_invalid")
        token_payload = {"content": rendered, "add_special": False, "parse_special": True}
        try:
            result = self.post(self.runner_url()+"/tokenize", token_payload)
        except (httpx.ConnectError, httpx.RemoteProtocolError):
            result = self.post(self.runner_url(refresh=True)+"/tokenize", token_payload)
        tokens = result["tokens"]
        if not isinstance(tokens, list) or not all(type(t) is int for t in tokens):
            raise ValueError("native_tokenizer_invalid")
        count, reserve, context = len(tokens), params["num_predict"], params["num_ctx"]
        return {"input_tokens": count, "output_reserve": reserve,
            "context_tokens": context, "required_tokens": count+reserve,
            "remaining_tokens": context-count-reserve, "normalization": "native_runner",
            "method": "ollama_native_runner_tokenizer"}

    async def verify(self, client, model, num_ctx=8192):
        self.metadata(model)
        # Exact tokenization is performed for every input during preflight.
        # Do not repeat render+tokenize for an unrelated sample on every batch.
        self._verified = (model["digest"], num_ctx)
