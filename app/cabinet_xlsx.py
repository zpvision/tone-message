import hashlib
import json
from pathlib import Path
import re
import time
import zipfile

from openpyxl import load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import column_index_from_string
from defusedxml.ElementTree import iterparse

MAX_BYTES = 30 * 1024 * 1024
MAX_UNPACKED = 120 * 1024 * 1024
MAX_CELLS = 500000
MAX_ROWS = 50000
FIELDS = ["ai_label", "ai_response", "ai_duration_ms", "ai_status", "ai_error", "ai_attempts",
          "ai_prompt_tokens", "ai_output_tokens", "ai_load_duration_ms", "ai_prompt_eval_duration_ms", "ai_eval_duration_ms"]


class WorkbookError(ValueError):
    pass


def validate(path):
    if Path(path).stat().st_size > MAX_BYTES:
        raise WorkbookError("Файл превышает 30 МБ")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 4000 or sum(m.file_size for m in members) > MAX_UNPACKED:
                raise WorkbookError("Распакованная книга превышает 120 МБ или 4000 элементов")
            if any(m.flag_bits & 1 or m.file_size > 80 * 1024 * 1024 for m in members):
                raise WorkbookError("Зашифрованная или слишком большая часть XLSX")
            if any("vbaproject" in m.filename.lower() or ".." in m.filename.split("/") for m in members):
                raise WorkbookError("Макросы и небезопасные пути не поддерживаются")
            if "xl/workbook.xml" not in archive.namelist():
                raise WorkbookError("Это не книга XLSX")
            actual_cells = 0
            for member in members:
                if not re.fullmatch(r"xl/worksheets/[^/]+\.xml", member.filename):
                    continue
                row_count = 0
                with archive.open(member) as source:
                    for _, element in iterparse(source, events=("end",)):
                        tag = element.tag.rsplit("}", 1)[-1]
                        if tag == "c":
                            actual_cells += 1
                            coordinate = element.get("r", "A1")
                            match = re.fullmatch(r"([A-Z]+)([0-9]+)", coordinate)
                            if not match or column_index_from_string(match[1]) > 256 or int(match[2]) > MAX_ROWS + 1 or actual_cells > MAX_CELLS:
                                raise WorkbookError("Фактические ячейки превышают лимиты XLSX")
                        elif tag == "row":
                            row_count += 1
                            if row_count > MAX_ROWS + 1:
                                raise WorkbookError("Более 50 001 фактической строки")
                        element.clear()
        book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        sheets, cells = [], 0
        try:
            if len(book.worksheets) > 30:
                raise WorkbookError("В книге более 30 листов")
            for sheet in book:
                if sheet.max_column > 256 or sheet.max_row > MAX_ROWS + 1:
                    raise WorkbookError("Лист превышает 256 колонок или 50 001 строку")
                # Scan actual cells as well as dimensions (dimensions can be forged).
                count = columns = 0
                for row in sheet.iter_rows():
                    count += 1
                    columns = max(columns, len(row))
                    cells += len(row)
                    if count > MAX_ROWS + 1 or columns > 256 or cells > MAX_CELLS:
                        raise WorkbookError("Превышен лимит строк или 500 000 ячеек книги")
                sheets.append({"name": sheet.title, "rows": count, "columns": columns})
        finally:
            book.close()
        with open(path, "rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        return {"sheets": sheets, "sha256": digest}
    except WorkbookError:
        raise
    except Exception as error:
        raise WorkbookError("Не удалось прочитать XLSX: повреждённый или неподдерживаемый файл") from error


def preview(path, sheet_name, headers):
    book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    try:
        if sheet_name not in book.sheetnames:
            raise WorkbookError("Лист не найден")
        sheet = book[sheet_name]
        rows = []
        first = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
        for row in sheet.iter_rows(min_row=2 if headers else 1, max_row=min(sheet.max_row, 11 if headers else 10), values_only=True):
            rows.append([str(value) if value is not None else "" for value in row])
        return {"columns": [{"index": i + 1, "name": f"{get_column_letter(i+1)} · {str(value)[:120]}" if headers else get_column_letter(i+1)} for i, value in enumerate(first)],
                "rows": rows, "total": max(0, sheet.max_row - int(headers))}
    finally:
        book.close()


def prepare_rows(store, job_id, selection):
    path = store.root / "uploads" / (selection["upload_id"] + ".xlsx")
    book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    try:
        sheet = book[selection["sheet"]]
        start = 2 if selection["headers"] else 1
        total = max(0, sheet.max_row - start + 1)
        count = min(total, selection["limit"]) if selection["limit"] is not None else total
        if count < 1 or count > MAX_ROWS or selection["column"] > sheet.max_column:
            raise WorkbookError("Нет строк для обработки, неверная колонка или превышен лимит 50 000")
        with store.connect() as db:
            for source_row, row in enumerate(sheet.iter_rows(min_row=start, max_row=start + count - 1,
                                       min_col=selection["column"], max_col=selection["column"]), start):
                cell = row[0]
                value = "" if cell.value is None else str(cell.value)
                status, error = ("skipped", "empty") if not value.strip() else ("pending", "")
                if cell.data_type == "f":
                    status, error = "input_error", "formula_not_evaluated"
                db.execute("INSERT OR IGNORE INTO rows(job_id,row_num,text,status,result) VALUES(?,?,?,?,?)",
                           (job_id, source_row, value, status, json.dumps({"error": error})))
    finally:
        book.close()


def unique(base, existing, limit=255):
    candidate, number = base[:limit], 2
    while candidate.casefold() in existing:
        suffix = f"_{number}"
        candidate, number = base[:limit-len(suffix)] + suffix, number + 1
    existing.add(candidate.casefold())
    return candidate


def text_cell(cell, value):
    value = str(value)
    clean = ILLEGAL_CHARACTERS_RE.sub("", value)
    overflow = len(clean.encode("utf-16-le")) // 2 > 32767 or clean != value
    if overflow:
        clean = clean[:15000] + "\n[Полное значение сохранено в JSONL]"
    cell.value = clean
    cell.data_type = "s"
    return overflow


def export_result(store, job_id, metrics):
    export_start = time.monotonic()
    job = store.job(job_id)
    selection = job["snapshot"]["selection"]
    source = store.root / "uploads" / (job["upload_id"] + ".xlsx")
    folder = store.root / "jobs" / job_id
    folder.mkdir(exist_ok=True, mode=0o700)
    book = load_workbook(source, data_only=False, keep_links=True)
    try:
        sheet = book[selection["sheet"]]
        columns = sheet.max_column
        if not selection["headers"]:
            sheet.insert_rows(1)
            for index in range(1, columns + 1):
                text_cell(sheet.cell(1, index), get_column_letter(index))
        existing = {str(cell.value).casefold() for cell in sheet[1] if cell.value is not None}
        output_names = [unique(field, existing) for field in FIELDS]
        for index, name in enumerate(output_names, columns + 1):
            cell = sheet.cell(1, index)
            text_cell(cell, name)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="164E63")
        with (folder / "result.jsonl.tmp").open("w", encoding="utf-8") as output:
            with store.connect() as db:
                for stored in db.execute("SELECT * FROM rows WHERE job_id=? ORDER BY row_num", (job_id,)):
                    row = dict(stored)
                    result = json.loads(row["result"])
                    output.write(json.dumps({**row, "result": result}, ensure_ascii=False) + "\n")
                    target_row = row["row_num"] + int(not selection["headers"])
                    data = [result.get("label", ""), result.get("response", ""), round(row["duration"] * 1000, 3),
                            row["status"], result.get("error", ""), row["attempts"], result.get("prompt_tokens", ""),
                            result.get("output_tokens", ""), result.get("load_ms", ""), result.get("prompt_eval_ms", ""), result.get("eval_ms", "")]
                    response_overflow = False
                    for offset, value in enumerate(data):
                        cell = sheet.cell(target_row, columns + offset + 1)
                        if isinstance(value, (int, float)):
                            cell.value = value
                        else:
                            changed = text_cell(cell, value)
                            response_overflow |= changed
                    if response_overflow:
                        text_cell(sheet.cell(target_row, columns + 4), row["status"] + ";full_value_in_jsonl")
                        text_cell(sheet.cell(target_row, columns + 5), result.get("error", "") + "; полный ответ/значение в JSONL")
        (folder / "result.jsonl.tmp").replace(folder / "result.jsonl")
        names = {name.casefold() for name in book.sheetnames}
        info = book.create_sheet(unique("Параметры теста", names, 31))
        text_cell(info.cell(1, 1), "Параметр")
        text_cell(info.cell(1, 2), "Значение")
        records = {"job_id": job_id, "created": job["created"], "state_at_export": job["status"],
                   "snapshot": job["snapshot"], "metrics": metrics,
                   "output_columns": dict(zip(FIELDS, output_names)),
                   "no_headers_policy": "Добавлена первая строка; исходные строки сдвинуты на +1" if not selection["headers"] else "Исходные номера строк сохранены",
                   "limitations": "openpyxl может изменить сложное оформление, диаграммы, формульные кеши и неподдерживаемые расширения Excel. Формулы и внешние ссылки сервер не вычисляет."}
        for key, value in records.items():
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            for number, start in enumerate(range(0, max(1, len(text)), 15000), 1):
                index = info.max_row + 1
                text_cell(info.cell(index, 1), f"{key} [{number}]")
                text_cell(info.cell(index, 2), text[start:start+15000])
        info.column_dimensions["A"].width = 28
        info.column_dimensions["B"].width = 110
        book.save(folder / "result.tmp.xlsx")
        # First full workbook serialization has now completed. Record its real
        # wall time, then commit metadata. The API/metrics JSON also includes
        # the final metadata commit in its complete export and total timings.
        measured = time.monotonic() - export_start
        snapshot_metrics = {**metrics,
            "export_seconds": metrics.get("export_seconds", 0) + measured,
            "total_seconds": metrics.get("total_seconds", 0) + measured,
            "timing_scope": "После полной записи XLSX, перед финальным сохранением метаданных. Полные времена включая этот шаг — в карточке и metrics.json."}
        for row in info:
            if row[0].value == "metrics [1]":
                text_cell(row[1], json.dumps(snapshot_metrics, ensure_ascii=False))
                break
        book.save(folder / "result.tmp.xlsx")
        (folder / "result.tmp.xlsx").replace(folder / "result.xlsx")
    finally:
        book.close()
