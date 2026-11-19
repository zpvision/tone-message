import asyncio

import httpx

from app.config import Settings


async def check_readiness(settings: Settings, client: httpx.AsyncClient):
    deadline = asyncio.get_running_loop().time() + settings.readiness_timeout_seconds
    result = {
        "status": "not_ready",
        "inference": {"available": None},
        "model": {"name": settings.model_id, "installed": None, "loaded": None},
    }
    if not settings.inference_url or not settings.model_id:
        return 503, {**result, "reason": "model_not_configured"}

    async def loaded():
        try:
            response = await client.get("/api/ps")
            response.raise_for_status()
            models = response.json()["models"]
            if not isinstance(models, list):
                return None
            return any(model.get("name") == settings.model_id or
                       model.get("model") == settings.model_id for model in models)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return None

    try:
        # One total deadline covers both lightweight requests. No generation.
        async with asyncio.timeout(settings.readiness_timeout_seconds):
            response = await client.get("/api/tags")
            result["inference"]["available"] = True
            response.raise_for_status()
            models = response.json()["models"]
            if not isinstance(models, list) or not all(isinstance(m, dict) for m in models):
                raise ValueError("Invalid model list")
            selected = next((model for model in models if
                model.get("name") == settings.model_id or
                model.get("model") == settings.model_id), None)
            result["model"]["installed"] = selected is not None
            if selected is not None and settings.model_digest and selected.get("digest") != settings.model_digest:
                return 503, {**result, "reason": "model_digest_mismatch"}
    except (httpx.RequestError, TimeoutError):
        result["inference"]["available"] = False
        return 503, {**result, "reason": "inference_unavailable"}
    except (httpx.HTTPStatusError, ValueError, KeyError, TypeError, AttributeError):
        return 503, {**result, "reason": "inference_check_failed"}

    # Resident state is informative. A failure of /api/ps cannot undo a valid
    # installed-model check; both requests share a bounded time budget.
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining > 0:
        try:
            async with asyncio.timeout(min(remaining, settings.readiness_timeout_seconds / 3)):
                result["model"]["loaded"] = await loaded()
        except TimeoutError:
            pass

    if not result["model"]["installed"]:
        return 503, {**result, "reason": "model_not_installed"}
    result["status"] = "ready"
    return 200, result
