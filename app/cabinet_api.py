import asyncio
import json
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.cabinet_engine import metrics
from app.cabinet_models import JobRequest, Prompts
from app.cabinet_store import ACTIVE, now
from app.cabinet_xlsx import MAX_BYTES, WorkbookError, preview, validate

router = APIRouter()
ID = re.compile(r"^[a-f0-9]{32}$")


def identified(value):
    if not ID.fullmatch(value):
        raise HTTPException(404, "Объект не найден")
    return value


def upload_record(store, value):
    row = store.one("SELECT * FROM uploads WHERE id=?", (identified(value),))
    if not row:
        raise HTTPException(404, "Файл не найден")
    return row


def job_record(store, value):
    row = store.job(identified(value))
    if not row:
        raise HTTPException(404, "Задание не найдено")
    return row


@router.get("/api/config")
def config(request: Request):
    store = request.app.state.cabinet_store
    latest = store.one("SELECT * FROM prompts ORDER BY id DESC LIMIT 1")
    return {"model": request.app.state.cabinet_engine.settings.model_id, "ollama_num_parallel": request.app.state.cabinet_engine.settings.ollama_num_parallel,
            "limits": {"upload_mb": 30, "rows": 50000, "num_ctx": 4096, "unpacked_mb": 120, "cells": 500000},
            "prompts": json.loads(latest["body"]) if latest else Prompts().model_dump(), "prompt_version": latest["id"] if latest else None}


@router.get("/api/prompts")
def get_prompts(request: Request):
    return [{"id": row["id"], "created": row["created"], **json.loads(row["body"])}
            for row in request.app.state.cabinet_store.all("SELECT * FROM prompts ORDER BY id DESC LIMIT 100")]


@router.post("/api/prompts")
def save_prompts(body: Prompts, request: Request):
    store = request.app.state.cabinet_store
    with store.connect() as db:
        cursor = db.execute("INSERT INTO prompts(created,body) VALUES(?,?)", (now(), body.model_dump_json()))
        return {"id": cursor.lastrowid, "created": now()}


@router.post("/api/uploads")
async def upload(request: Request, file: UploadFile):
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(400, "Принимаются только .xlsx")
    store = request.app.state.cabinet_store
    # Keep the small shared test server below an explicit storage ceiling.
    if shutil.disk_usage(store.root).free < 5 * 1024**3 or store.one("SELECT coalesce(sum(size),0) n FROM uploads")["n"] > 5 * 1024**3:
        raise HTTPException(507, "Недостаточно места. Удалите старые задания и файлы")
    upload_id = uuid.uuid4().hex
    path = store.root / "uploads" / (upload_id + ".xlsx")
    count = 0
    try:
        with path.open("xb") as output:
            while chunk := await file.read(1024 * 1024):
                count += len(chunk)
                if count > MAX_BYTES:
                    raise HTTPException(413, "Файл превышает 30 МБ")
                output.write(chunk)
        metadata = await asyncio.to_thread(validate, path)
        name = (file.filename or "book.xlsx").replace("\\", "/").split("/")[-1][:180]
        store.execute("INSERT INTO uploads(id,name,created,size,meta) VALUES(?,?,?,?,?)",
                      (upload_id, name, now(), count, json.dumps(metadata, ensure_ascii=False)))
        path.chmod(0o400)
        return {"id": upload_id, "name": name, **metadata}
    except WorkbookError as error:
        path.unlink(missing_ok=True)
        raise HTTPException(400, str(error)) from error
    except asyncio.CancelledError:
        path.unlink(missing_ok=True)
        raise
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@router.get("/api/uploads/{upload_id}/preview")
def preview_file(upload_id: str, sheet: str, request: Request, headers: bool = True):
    store = request.app.state.cabinet_store
    upload_record(store, upload_id)
    try:
        return preview(store.root / "uploads" / (upload_id + ".xlsx"), sheet, headers)
    except WorkbookError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/api/jobs")
async def start(body: JobRequest, request: Request):
    store = request.app.state.cabinet_store
    record = upload_record(store, body.upload_id)
    metadata = json.loads(record["meta"])
    sheet = next((s for s in metadata["sheets"] if s["name"] == body.sheet), None)
    if not sheet or body.column > sheet["columns"]:
        raise HTTPException(400, "Проверьте лист и колонку")
    available = max(0, sheet["rows"] - int(body.headers))
    selected = min(available, body.limit) if body.limit else available
    if not selected or selected > 50000:
        raise HTTPException(400, "Нужно выбрать от 1 до 50 000 строк")
    try:
        job_id = await request.app.state.cabinet_engine.start(body, record)
        return {"id": job_id}
    except (ValueError, sqlite3.IntegrityError) as error:
        raise HTTPException(409, str(error) if isinstance(error, ValueError) else "Есть активное задание") from error
    except Exception as error:
        raise HTTPException(503, "Ollama недоступна: проверьте готовность") from error


@router.get("/api/jobs")
def history(request: Request):
    store = request.app.state.cabinet_store
    return [{"id": row["id"], "created": row["created"], "status": row["status"],
             "filename": json.loads(row["snapshot"])["selection"]["filename"], "export_ready": bool(row["export_ready"])}
            for row in store.all("SELECT * FROM jobs ORDER BY created DESC LIMIT 200")]


@router.get("/api/jobs/{job_id}")
def detail(job_id: str, request: Request):
    store = request.app.state.cabinet_store
    job = job_record(store, job_id)
    engine = request.app.state.cabinet_engine
    timing = engine.live_timing() if engine.current == job_id else job["timing"]
    sample = store.one("SELECT data FROM samples WHERE job_id=? ORDER BY rowid DESC LIMIT 1", (job_id,))
    return {**job, "metrics": metrics(store, job_id, timing), "resources": json.loads(sample["data"]) if sample else None,
            "recent": [{**row, "result": json.loads(row["result"])} for row in store.all(
                "SELECT row_num,status,duration,attempts,result FROM rows WHERE job_id=? AND status NOT IN ('pending','processing') ORDER BY row_num DESC LIMIT 10", (job_id,))]}


@router.post("/api/jobs/{job_id}/stop")
async def stop(job_id: str, request: Request):
    job_record(request.app.state.cabinet_store, job_id)
    try:
        request.app.state.cabinet_engine.stop(job_id)
        return {"status": "stopping"}
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/api/jobs/{job_id}/resume")
async def resume(job_id: str, request: Request):
    job_record(request.app.state.cabinet_store, job_id)
    try:
        await request.app.state.cabinet_engine.resume(job_id)
        return {"id": job_id}
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except Exception as error:
        raise HTTPException(503, "Не удалось проверить модель") from error


@router.get("/api/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str, request: Request):
    store = request.app.state.cabinet_store
    job = job_record(store, job_id)
    if kind not in ("xlsx", "jsonl", "metrics"):
        raise HTTPException(404, "Формат не найден")
    if kind == "metrics":
        if job["status"] in ACTIVE:
            raise HTTPException(409, "Итоговые метрики доступны после остановки или завершения")
        data = {"id": job_id, "snapshot": job["snapshot"], "metrics": metrics(store, job_id),
                "attempts": store.all("SELECT * FROM attempts WHERE job_id=? ORDER BY row_num,number", (job_id,)),
                "samples": [{"elapsed": row["elapsed"], **json.loads(row["data"])} for row in store.all("SELECT * FROM samples WHERE job_id=?", (job_id,))]}
        from fastapi.responses import Response
        return Response(json.dumps(data, ensure_ascii=False), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{job_id}-metrics.json"'})
    if not job["export_ready"]:
        raise HTTPException(409, "Результат ещё готовится. Строки уже сохранены в SQLite")
    path = store.root / "jobs" / job_id / ("result." + kind)
    if not path.is_file():
        raise HTTPException(404, "Файл пока недоступен")
    return FileResponse(path, filename=f"{job_id}-result.{kind}", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if kind == "xlsx" else "application/x-ndjson")


@router.delete("/api/jobs/{job_id}")
async def delete(job_id: str, request: Request, confirm: bool = False):
    if not confirm:
        raise HTTPException(400, "Нужно подтвердить удаление")
    store = request.app.state.cabinet_store
    engine = request.app.state.cabinet_engine
    async with engine.lock:
        job = job_record(store, job_id)
        if job["status"] in ACTIVE or engine.current == job_id:
            raise HTTPException(409, "Сначала остановите задание")
        with store.connect() as db:
            for table in ("rows", "attempts", "samples"):
                db.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        folder = store.root / "jobs" / job_id
        if folder.exists():
            shutil.rmtree(folder)
        if not store.one("SELECT id FROM jobs WHERE upload_id=?", (job["upload_id"],)):
            (store.root / "uploads" / (job["upload_id"] + ".xlsx")).unlink(missing_ok=True)
            store.execute("DELETE FROM uploads WHERE id=?", (job["upload_id"],))
        return {"deleted": True}


@router.post("/api/jobs/{job_id}/export")
async def partial_export(job_id: str, request: Request):
    from app.cabinet_xlsx import export_result
    engine = request.app.state.cabinet_engine
    store = request.app.state.cabinet_store
    async with engine.lock:
        job = job_record(store, job_id)
        if store.active() or engine.current:
            raise HTTPException(409, "Дождитесь завершения активного задания")
        if job["status"] not in ("interrupted", "stopped", "failed", "completed"):
            raise HTTPException(409, "Сначала остановите задание")
        store.set_job(job_id, status="exporting", export_ready=0)
        try:
            await asyncio.to_thread(export_result, store, job_id, metrics(store, job_id))
            store.set_job(job_id, status=job["status"], export_ready=1)
        except Exception as error:
            store.set_job(job_id, status=job["status"], error="partial_export_failed")
            raise HTTPException(500, "Не удалось создать частичный XLSX; строки сохранены в базе") from error
    return {"export_ready": True}
