import uvicorn

from app.config import Settings


if __name__ == "__main__":
    settings = Settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=30,
        log_level="info",
    )
