from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import Settings
from app.readiness import check_readiness

def create_app(settings: Settings | None = None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application):
        async with httpx.AsyncClient(
            base_url=settings.inference_url or "",
            timeout=settings.readiness_timeout_seconds,
            trust_env=False,
        ) as client:
            application.state.inference_client = client
            engine = None
            if settings.cabinet_enabled:
                from app.cabinet_engine import Engine
                from app.cabinet_store import Store
                store = Store(settings.cabinet_data_dir)
                store.recover()
                from app.load_control import LoadControl
                control = LoadControl(lambda: bool(store.active()) or bool(engine and engine.task and not engine.task.done()),
                                      settings.api_max_waiting_batches,settings.api_max_waiting_messages,
                                      settings.inference_concurrency,settings.task_priorities,settings.task_aging_seconds)
                engine = Engine(store, settings, control)
                engine.inference.log_effective()
                # Fail closed on a missing model; do not select another installed model.
                import asyncio
                import logging
                for attempt in range(3):
                    try:
                        model = await engine.describe_model()
                        logging.getLogger("uvicorn.error").info("effective_model name=%s digest=%s quantization=%s",
                            model["name"], model["digest"], model["quantization"])
                        break
                    except httpx.HTTPError as exc:
                        if attempt < 2:
                            await asyncio.sleep(.5)
                            continue
                        logging.getLogger("uvicorn.error").error("inference_startup_check_failed type=%s model=%s",
                            type(exc).__name__, settings.model_id)
                    except Exception as exc:
                        logging.getLogger("uvicorn.error").error("inference_startup_check_failed type=%s model=%s",
                            type(exc).__name__, settings.model_id)
                        break
                application.state.cabinet_store = store
                application.state.cabinet_engine = engine
            if settings.api_enabled:
                from app.api_keys import KeyStore
                from app.batch_api import BatchService
                if engine is None:
                    raise RuntimeError('API requires the existing cabinet engine')
                application.state.api_keys = KeyStore(settings.api_data_dir)
                application.state.batch_service = BatchService(settings,engine)
            try:
                yield
            finally:
                if engine:
                    await engine.close()

    application = FastAPI(
        title="FinKoper AI API",
        description="Пакетный анализ тональности сообщений через настраиваемый inference backend.",
        docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    @application.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @application.get("/readyz")
    async def readyz(request: Request):
        code, body = await check_readiness(settings, request.app.state.inference_client)
        return JSONResponse(status_code=code, content=body)

    if settings.cabinet_enabled:
        from pathlib import Path
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
        from app.cabinet_api import router
        application.include_router(router)
        static = Path(__file__).parent / "static"
        application.mount("/static", StaticFiles(directory=static), name="static")

        @application.get("/")
        async def home():
            return FileResponse(static / "index.html")

        @application.middleware("http")
        async def same_origin(request, call_next):
            # Basic Auth browsers send credentials automatically; require a
            # non-simple header and same origin for all state-changing calls.
            from app.batch_api import PATHS
            if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.url.path not in PATHS:
                from urllib.parse import urlsplit
                origin = request.headers.get("origin")
                if request.headers.get("x-cabinet-request") != "1" or (origin and urlsplit(origin).netloc != request.headers.get("host")):
                    return JSONResponse(status_code=403, content={"detail": "Недопустимый источник запроса"})
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

    if settings.api_enabled:
        from app.batch_api import ApiBoundary, router as batch_router
        application.include_router(batch_router)
        application.add_middleware(ApiBoundary,settings=settings)
        from app.public_docs import install_public_docs
        install_public_docs(application,settings)
    return application


app = create_app()
