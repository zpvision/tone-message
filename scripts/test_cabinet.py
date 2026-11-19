import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app.cabinet_engine import Engine, metrics
from app.cabinet_models import JobRequest, Prompts
from app.cabinet_store import Store
from app.cabinet_xlsx import export_result, prepare_rows, validate
from app.config import Settings
from app.main import create_app


def book_bytes():
    book = Workbook()
    sheet = book.active
    sheet.title = "Сообщения"
    sheet.append(["ID", "Текст", "ai_label"])
    sheet.append([1, "Спасибо!", "original"])
    sheet.append([2, None, "original"])
    sheet.append([3, "=1+1", "original"])
    sheet.append([4, "Вы опоздали", "original"])
    sheet.append([5, "а " * 5000, "original"])
    book.create_sheet("Другой лист").append(["Не менять", 42])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


class CabinetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.upload_id = "a" * 32
        self.job_id = "b" * 32
        path = self.store.root / "uploads" / (self.upload_id + ".xlsx")
        path.write_bytes(book_bytes())
        self.meta = validate(path)
        self.snapshot = {"selection": {"upload_id": self.upload_id, "sheet": "Сообщения", "column": 2,
                                      "headers": True, "limit": None, "filename": "sample.xlsx"},
                         "parameters": {"temperature": 0, "num_ctx": 4096, "num_predict": 128, "thinking": False, "concurrency": 1, "timeout": 10},
                         "prompts": {"system": 'JSON {"label":"…"}', "template": "Другие {скобки}; сообщение: {text}"},
                         "model": {"name": "qwen3:8b", "digest": "test"}, "ollama_num_parallel": 1}
        self.store.add_job(self.job_id, self.snapshot)

    def tearDown(self):
        self.temp.cleanup()

    async def test_results_errors_original_and_excel_safety(self):
        prepare_rows(self.store, self.job_id, self.snapshot["selection"])
        seen = []
        async def inference(request):
            payload = json.loads(request.content)
            seen.append(payload)
            self.assertIn("{скобки}", payload["messages"][1]["content"])
            text = payload["messages"][1]["content"]
            content = '{"label":"positive"}' if "Спасибо" in text else "=не JSON"
            count = engine.context_budget.count(payload['messages'][0]['content'], text, self.snapshot['parameters'])['input_tokens']
            return httpx.Response(200, json={"done": True, "done_reason": "stop", "message": {"content": content}, "eval_count": 8, "prompt_eval_count": count})
        engine = Engine(self.store, Settings())
        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(inference)) as client:
            await engine.worker(self.job_id, self.snapshot, client)
        self.assertEqual(len(seen), 2)
        rows = self.store.all("SELECT * FROM rows WHERE job_id=? ORDER BY row_num", (self.job_id,))
        self.assertEqual([r["status"] for r in rows], ["ok", "skipped", "input_error", "parse_error", "input_error"])
        export_result(self.store, self.job_id, metrics(self.store, self.job_id))
        result = load_workbook(self.store.root / "jobs" / self.job_id / "result.xlsx", data_only=False)
        original = load_workbook(self.store.root / "uploads" / (self.upload_id + ".xlsx"), data_only=False)
        self.assertEqual(result.sheetnames[:2], original.sheetnames)
        for name in original.sheetnames:
            for row in original[name]:
                for cell in row:
                    self.assertEqual(result[name][cell.coordinate].value, cell.value)
        names = [c.value for c in result["Сообщения"][1]]
        self.assertIn("ai_label_2", names)
        self.assertEqual(result["Сообщения"].cell(5, names.index("ai_response") + 1).data_type, "s")
        result.close()
        original.close()
        self.assertEqual(len((self.store.root / "jobs" / self.job_id / "result.jsonl").read_text().splitlines()), 5)

    async def test_overflow_response_is_preserved_in_jsonl(self):
        prepare_rows(self.store, self.job_id, self.snapshot["selection"])
        content = "=" + "я" * 40000
        self.store.finish_row(self.job_id, 2, "parse_error", {"response": content, "error": "invalid_json_label"})
        export_result(self.store, self.job_id, {})
        result = load_workbook(self.store.root / "jobs" / self.job_id / "result.xlsx")
        names = [c.value for c in result["Сообщения"][1]]
        status = result["Сообщения"].cell(2, names.index("ai_status") + 1).value
        self.assertIn("full_value_in_jsonl", status)
        self.assertEqual(result["Сообщения"].cell(2, names.index("ai_response") + 1).data_type, "s")
        result.close()
        row = json.loads((self.store.root / "jobs" / self.job_id / "result.jsonl").read_text().splitlines()[0])
        self.assertEqual(row["result"]["response"], content)

    async def test_restart_and_single_active_job(self):
        prepare_rows(self.store, self.job_id, self.snapshot["selection"])
        row = self.store.claim(self.job_id)
        self.store.begin_attempt(self.job_id, row["row_num"])
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_job("c" * 32, self.snapshot)
        self.store.recover()
        self.assertEqual(self.store.job(self.job_id)["status"], "interrupted")
        self.assertEqual(self.store.one("SELECT status FROM rows WHERE job_id=? AND row_num=2", (self.job_id,))["status"], "pending")
        self.assertEqual(metrics(self.store, self.job_id)["incomplete_attempts"], 1)

    async def test_no_headers_keeps_values_and_order(self):
        self.snapshot["selection"]["headers"] = False
        self.store.execute("UPDATE jobs SET snapshot=? WHERE id=?", (json.dumps(self.snapshot), self.job_id))
        prepare_rows(self.store, self.job_id, self.snapshot["selection"])
        export_result(self.store, self.job_id, {})
        result = load_workbook(self.store.root / "jobs" / self.job_id / "result.xlsx")
        self.assertEqual(result["Сообщения"].cell(2, 2).value, "Текст")
        self.assertEqual(result["Сообщения"].cell(3, 2).value, "Спасибо!")
        result.close()

    async def test_network_retry_but_never_retry_parse_error(self):
        prepare_rows(self.store, self.job_id, self.snapshot["selection"])
        calls = 0
        async def upstream(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("synthetic", request=request)
            payload = json.loads(request.content)
            count = engine.context_budget.count(payload['messages'][0]['content'], payload['messages'][1]['content'], self.snapshot['parameters'])['input_tokens']
            return httpx.Response(200, json={"done": True, "done_reason": "stop", "message": {"content": "bad"}, "prompt_eval_count": count})
        engine = Engine(self.store, Settings())
        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(upstream)) as client:
            await engine.worker(self.job_id, self.snapshot, client)
        self.assertEqual(calls, 3)
        self.assertEqual(self.store.one("SELECT attempts FROM rows WHERE job_id=? AND row_num=2", (self.job_id,))["attempts"], 2)
        self.assertEqual(self.store.one("SELECT status FROM rows WHERE job_id=? AND row_num=2", (self.job_id,))["status"], 'parse_error')

    async def test_long_russian_prompt_ok_and_token_mismatch_is_warning(self):
        self.snapshot['prompts']['system'] = 'Определи тональность сообщения клиента. ' * 70
        self.assertGreater(len(self.snapshot['prompts']['system'].encode()), 4096)
        prepare_rows(self.store, self.job_id, self.snapshot['selection'])
        engine = Engine(self.store, Settings())
        async def upstream(request):
            payload = json.loads(request.content)
            self.assertFalse(payload['truncate'])
            self.assertFalse(payload['shift'])
            count = engine.context_budget.count(payload['messages'][0]['content'], payload['messages'][1]['content'], self.snapshot['parameters'])['input_tokens']
            if 'Вы опоздали' in payload['messages'][1]['content']:
                count -= 1
            return httpx.Response(200, json={'done': True, 'done_reason': 'stop', 'message': {'content': '{"label":"positive"}'}, 'prompt_eval_count': count})
        async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(upstream)) as client:
            await engine.worker(self.job_id, self.snapshot, client)
        rows = self.store.all('SELECT status,result FROM rows WHERE job_id=? ORDER BY row_num', (self.job_id,))
        self.assertEqual(rows[0]['status'], 'ok')
        self.assertEqual(rows[3]['status'], 'ok')
        mismatch = json.loads(rows[3]['result'])
        self.assertEqual(mismatch['error'], '')
        self.assertEqual(mismatch['warnings'][0]['code'], 'context_token_count_mismatch')
        self.assertEqual(mismatch['warnings'][0]['delta'], -1)
        self.assertEqual(mismatch['label'], 'positive')
        self.assertEqual(mismatch['response'], '{"label":"positive"}')


class ApiTests(unittest.TestCase):
    def test_upload_prompts_limits_and_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(cabinet_enabled=True, cabinet_data_dir=directory)
            with TestClient(create_app(settings)) as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.post("/api/prompts", json=Prompts().model_dump()).status_code, 403)
                headers = {"X-Cabinet-Request": "1"}
                prompts = {"system": 'JSON {"a":1}', "template": "{other} {text}"}
                self.assertEqual(client.post("/api/prompts", json=prompts, headers=headers).status_code, 200)
                self.assertEqual(client.get("/api/config").json()["prompts"], prompts)
                response = client.post("/api/uploads", files={"file": ("synthetic.xlsx", book_bytes())}, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                upload = response.json()
                preview = client.get(f'/api/uploads/{upload["id"]}/preview', params={"sheet": "Сообщения", "headers": "true"}).json()
                self.assertEqual(preview["total"], 5)
                self.assertEqual(preview["columns"][1]["index"], 2)
                self.assertEqual(client.get("/api/jobs/not-an-id/download/xlsx").status_code, 404)
                self.assertEqual(client.post("/api/uploads", files={"file": ("fake.xlsx", b"bad")}, headers=headers).status_code, 400)
                with self.assertRaises(ValueError):
                    JobRequest(upload_id=upload["id"], sheet="Сообщения", column=2, parameters={"num_ctx": 32768})


if __name__ == "__main__":
    unittest.main(verbosity=2)
