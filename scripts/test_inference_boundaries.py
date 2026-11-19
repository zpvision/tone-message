"""Configuration/digest readiness and one total GPU wait budget. No model generation."""
import asyncio
import tempfile
import time
import unittest
import httpx
from pydantic import ValidationError
from app.config import Settings
from app.inference import GpuGate
from app.readiness import check_readiness

class ConfigBoundaryTests(unittest.TestCase):
    def test_digest_format_and_empty_optional_pin(self):
        self.assertIsNone(Settings(model_digest="").model_digest)
        for value in ("not-a-digest", "a"*63, "g"*64):
            with self.subTest(value_length=len(value)):
                with self.assertRaises(ValidationError):
                    Settings(model_digest=value)
        self.assertEqual(Settings(model_digest="a"*64).model_digest,"a"*64)

class ReadinessBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_name_with_wrong_digest_is_not_ready(self):
        s=Settings(inference_url="http://synthetic.test",model_id="synthetic:model",model_digest="a"*64)
        async def upstream(request):
            return httpx.Response(200,json={"models":[{"name":"synthetic:model","digest":"b"*64}]})
        async with httpx.AsyncClient(base_url=s.inference_url,transport=httpx.MockTransport(upstream)) as client:
            status,body=await check_readiness(s,client)
        self.assertEqual(status,503)
        self.assertEqual(body["reason"],"model_digest_mismatch")
        self.assertTrue(body["model"]["installed"])

    async def test_matching_digest_is_ready(self):
        s=Settings(inference_url="http://synthetic.test",model_id="synthetic:model",model_digest="a"*64)
        async def upstream(request):
            return httpx.Response(200,json={"models":[{"name":"synthetic:model","digest":"a"*64}]})
        async with httpx.AsyncClient(base_url=s.inference_url,transport=httpx.MockTransport(upstream)) as client:
            status,body=await check_readiness(s,client)
        self.assertEqual(status,200)
        self.assertTrue(body["model"]["loaded"])

    async def test_missing_upstream_digest_with_pin_fails_closed(self):
        s=Settings(inference_url="http://synthetic.test",model_id="synthetic:model",model_digest="a"*64)
        async def upstream(request):
            return httpx.Response(200,json={"models":[{"name":"synthetic:model"}]})
        async with httpx.AsyncClient(base_url=s.inference_url,transport=httpx.MockTransport(upstream)) as client:
            status,body=await check_readiness(s,client)
        self.assertEqual((status,body.get("reason")),(503,"model_digest_mismatch"))

    async def test_gpu_wait_uses_one_total_budget(self):
        with tempfile.TemporaryDirectory() as root:
            settings=Settings(inference_lock_dir=root,inference_lock_timeout=.12)
            gate=GpuGate(settings)
            other=GpuGate(settings)
            await gate.local.acquire()
            async def release_local():
                await asyncio.sleep(.08)
                gate.local.release()
            release=asyncio.create_task(release_local())
            async with other.slot():
                started=time.monotonic()
                with self.assertRaises(TimeoutError):
                    async with gate.slot():
                        pass
                elapsed=time.monotonic()-started
            await release
            self.assertLess(elapsed,.18,"local wait and file wait must share the .12s budget")
            # A timeout must not leak either kind of permit.
            async with gate.slot():
                pass

if __name__=="__main__":
    unittest.main(verbosity=2)
