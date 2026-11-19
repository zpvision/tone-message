import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from pydantic import ValidationError
from app.config import Settings
from app.inference import GpuGate, Inference
from app.context_budget import ContextBudget
from app.load_control import LoadControl

class ConfigTests(unittest.TestCase):
    def test_invalid_config_and_no_model_default(self):
        for values in (dict(api_enabled=True), dict(inference_backend="unknown"),
                       dict(inference_keep_alive="forever"), dict(sentiment_workers=2),
                       dict(inference_url="http://user:secret@localhost")):
            with self.assertRaises(ValidationError): Settings(**values)
        self.assertIsNone(Settings().model_id)

    def test_keep_alive_numeric_encoding(self):
        self.assertEqual(Settings(inference_keep_alive="-1").keep_alive,-1)
        self.assertEqual(Settings(inference_keep_alive="0").keep_alive,0)
        self.assertEqual(Settings(inference_keep_alive="30m").keep_alive,"30m")

    def test_profiles_share_model(self):
        s=Settings(model_id="synthetic:g",function_profiles={"assistant":{"max_output":1024}})
        self.assertEqual(s.profile().max_output,128)
        self.assertEqual(s.profile("assistant").max_output,1024)
        with self.assertRaises(ValueError):s.profile("not_configured")

    def test_native_discovery_uses_selected_blob_and_caches(self):
        s=Settings(model_id="synthetic:g",inference_url="http://127.0.0.1:11434")
        c=ContextBudget(s)
        with patch.object(c,"post",return_value={"modelfile":"FROM /models/blob-selected"}), patch(
            "app.context_budget.subprocess.check_output",return_value=
            "/bin/llama-server --model /models/blob-other --host 127.0.0.1 --port 3333\n"
            "/bin/llama-server --model /models/blob-selected --host 127.0.0.1 --port 4444") as ps:
            self.assertEqual(c.runner_url(),"http://127.0.0.1:4444")
            self.assertEqual(c.runner_url(),"http://127.0.0.1:4444")
            self.assertEqual(ps.call_count,1)
        c.close()

class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_model_never_falls_back(self):
        s=Settings(model_id="synthetic:missing")
        adapter=Inference(s,None)
        async def upstream(r):
            return httpx.Response(200,json={"models":[{"name":"synthetic:other"}]})
        async with httpx.AsyncClient(base_url="http://test",transport=httpx.MockTransport(upstream)) as c:
            with self.assertRaisesRegex(ValueError,"not_installed"):await adapter.describe(c)

    async def test_cross_process_style_gate_timeout_cancel_release(self):
        with tempfile.TemporaryDirectory() as root:
            s=Settings(inference_lock_dir=root,inference_lock_timeout=.06)
            first,second=GpuGate(s),GpuGate(s)
            async with first.slot():
                with self.assertRaises(TimeoutError):
                    async with second.slot():pass
            async with second.slot():pass
            async with first.slot():pass

    async def test_aging_beats_new_higher_priority(self):
        gate=LoadControl(priorities={"sentiment":0,"assistant":1},aging_seconds=.01)
        entered=asyncio.Event(); release=asyncio.Event(); order=[]
        async def task(kind,hold=False):
            async with gate.slot(1,1,kind):
                order.append(kind)
                if hold:entered.set();await release.wait()
        a=asyncio.create_task(task("sentiment",True));await entered.wait()
        b=asyncio.create_task(task("assistant"));await asyncio.sleep(.03)
        c=asyncio.create_task(task("sentiment"));await asyncio.sleep(0)
        release.set();await asyncio.gather(a,b,c)
        self.assertEqual(order,["sentiment","assistant","sentiment"])

if __name__=="__main__":unittest.main(verbosity=2)
