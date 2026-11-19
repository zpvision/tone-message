# Model-neutral inference operations

Active API code: /opt/finkoper-ai/app. systemd: finkoper-ai.service (one Python worker).
Settings: /etc/finkoper-ai/finkoper-ai.env. Example: .env.example.
Model runner: Ollama 0.40.5 on 127.0.0.1:11434. Do not update CUDA/drivers for these changes.

## Model selection and diagnostics
Set FINKOPER_INFERENCE_BACKEND=ollama, FINKOPER_INFERENCE_URL=http://127.0.0.1:11434,
and FINKOPER_MODEL_ID to the exact installed model name (currently gemma4:26b).
FINKOPER_MODEL_DIGEST optionally pins its lowercase SHA-256 digest (64 hexadecimal characters); update it explicitly with a model change. An empty pin is allowed. /readyz returns 503 model_digest_mismatch if the installed digest differs or is absent.
There is no fallback or implicit model download. The public legacy model alias is independent
of model selection. Existing clients may continue sending qwen3:8b; no Qwen model runs.
New clients can send gemma4:26b. The response echoes the accepted public identifier.
Internal logs always record effective_model=gemma4:26b and startup records the digest.

Validate the ID using /api/tags and /api/show. Check /readyz and startup logs.
The installed exact-token counting uses Ollama's existing _debug_render_only extension and
the native /tokenize endpoint of its local runner. Runner/blob discovery is based on
/api/show for the selected model, not a compiled ID or architecture.
No render extension, ambiguous runner, missing model or digest mismatch fails closed.
Remote Ollama and other inference protocols require a tokenizer/backend adapter; they
are rejected rather than silently changing model or using a different tokenizer.
A future backend can implement the existing Inference describe/payload/chat interface.

## Profiles and GPU budget
Common: INFERENCE_KEEP_ALIVE, INFERENCE_CONCURRENCY, INFERENCE_LOCK_DIR,
INFERENCE_LOCK_TIMEOUT, INFERENCE_RETRIES (network errors only), INFERENCE_RETRY_DELAY,
CONNECT_TIMEOUT_SECONDS, TOKENIZER_TIMEOUT, TOKENIZER_DISCOVERY_TTL; all use FINKOPER_ prefix.
Sentiment: API_NUM_CTX, SENTIMENT_MAX_OUTPUT, SENTIMENT_TEMPERATURE,
SENTIMENT_THINKING, SENTIMENT_WORKERS, API_INFERENCE_TIMEOUT.
API limits retain their previous names and meanings: API_MAX_ITEMS, API_MAX_BODY_BYTES,
API_MAX_WAITING_BATCHES, API_MAX_WAITING_MESSAGES, API_QUEUE_TIMEOUT, API_DEADLINE.
One batch is atomic: no partial response or synthetic neutral on inference failure.
Timeouts do not retry. Transport failures have at most INFERENCE_RETRIES retries.

Example future configuration (no future feature is enabled):
FINKOPER_FUNCTION_PROFILES={"morning_briefing":{"num_ctx":8192,"max_output":1024,"workers":1},"assistant":{"num_ctx":8192,"max_output":512,"workers":1}}
Profiles select decoding/context parameters, not another model copy.
Call settings.profile(name), inference.payload(profile,...), inference.chat(...).
All processes must share INFERENCE_LOCK_DIR and INFERENCE_CONCURRENCY, with read/write
group access to lock files. The gate releases permits on cancellation/process exit.
Within the API, LoadControl.slot(task_type=...) supports TASK_PRIORITIES (lower wins)
and TASK_AGING_SECONDS. Wait and execution deadlines remain finite.
Do not bypass the shared adapter in future functions.

Selected 4090 settings: one inference slot, one sentiment worker, context 8192,
max_output 128, temperature 0, thinking false, keep_alive -1.
Two API workers on the current single-slot runner were slower in the synthetic 20-item
comparison (10.01s vs 9.35s), so higher parallelism was not enabled.
Ollama global guard: NUM_PARALLEL=1, MAX_LOADED_MODELS=1, MAX_QUEUE=32.
The native runner currently uses ~19.5GiB of 24GiB; do not create per-profile copies.
Open WebUI also uses Ollama and does not implement the cooperative file gate; the
Ollama slot/model/queue limits are the final guard for such callers.
CPU embeddings remain separate on 11435. CUDA_VISIBLE_DEVICES=-1 alone was insufficient:
this build auto-selected Vulkan and used ~771MiB VRAM. The cpu-only.conf drop-in adds
GGML_VK_VISIBLE_DEVICES=-1 and OLLAMA_VULKAN=false. Verified bge-m3:latest size_vram=0
and a successful 1024-dimensional synthetic embedding after the correction.

## Restart requirements
Every FINKOPER_* change requires systemctl restart finkoper-ai.
Changing backend context/parallelism/default keep_alive in the Ollama systemd drop-in
requires daemon-reload and restart ollama (affects all its callers), then warm the model.
Never increase Ollama NUM_PARALLEL based solely on GPU name: measure VRAM and latency.
A new model needs explicit install, exact ID/digest configuration and contract/quality checks;
changing model for performance requires separate approval.
Model pin prevents accidental replacement. /readyz reports installed model; unloaded
resident state is diagnostic, not an automatic fallback.

## Verification
Run PYTHONPATH=. /opt/finkoper-ai/venv/bin/python scripts/test_batch_api.py
and scripts/test_inference_config.py, scripts/test_readiness.py, scripts/test_public_docs.py.
Mock tests use synthetic data only. scripts/contract_live.py probes the external API;
scripts/benchmark_synthetic.py benchmarks synthetic 1/20/80-item batches.
Both create an in-memory temporary bearer and revoke it in finally. Never print it.
The benchmark's static prefix benefits KV caching; unique item numbers prevent identical
whole prompts within a batch. Repeated runs can still benefit KV cache, and identical
texts in single-request repetitions demonstrate this effect. No response cache exists.
Results are synthetic measurements, not validated real-conversation speed or quality.

## Rollback
Closed backup: /var/backups/finkoper-ai/20261118-model-neutral/before.tar.gz
Stop finkoper-ai, ollama and ollama-embeddings, restore with sudo tar -xzf BACKUP -C /,
restore helpers-before.tar.gz and remove the added model-neutral.conf and embeddings cpu-only.conf drop-ins,
systemctl daemon-reload, start ollama and finkoper-ai, then check /healthz, /readyz
and a synthetic external API request. The backup contains secrets: root access only,
never commit, copy into a report, or serve it.
Source baseline commit: c1643244f32c6cfd947b0824332b99557806af46.
The rollback script deploy/rollback-model-neutral.sh implements these steps.

Follow-up validation: INFERENCE_LOCK_TIMEOUT is one total budget for local semaphore plus cooperative file-slot waiting; it does not include inference execution. See scripts/test_inference_boundaries.py.
