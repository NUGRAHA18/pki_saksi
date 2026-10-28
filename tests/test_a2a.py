"""Tes Fase 3: server & klien A2A (in-memory lewat ASGITransport, tanpa port/jaringan)."""
import asyncio
import logging
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from agents.common.a2a_client import A2AClient, A2AError, artifact_data, status_text
from agents.common.a2a_server import (TASK_NOT_FOUND, UNSUPPORTED_OPERATION, A2AServer, Completed, Failed,
                                      InputRequired, agent_card, data_part, make_artifact, text_part)
from agents.common.audit import AuditLog
from agents.common.llm import ToolCall
from agents.common.mcp_client import MCPHub
from agents.worker import ANSWER_TOOL
from agents.worker_server import ARTIFACT, build_server
from mcp_servers import cekfakta, fetch, wiki
from tests.test_mcp_servers import ServerTestCase
from tests.test_worker import SEARCH, FakeLLM, answer

logging.getLogger("asyncio").setLevel(logging.ERROR)  # peringatan "took 0.1 seconds" dari mode debug asyncio tes


async def echo(ctx):
    """Handler uji: meminta nama bila kosong (input-required), gagal/lambat sesuai perintah, selain itu menggema."""
    text = ctx.text()
    if text == "crash":
        raise ValueError("rusak")
    if text == "lambat":
        await asyncio.sleep(5)
    if text == "gagal":
        return Failed("tidak bisa")
    if not text:
        return InputRequired("Siapa namamu?")
    turns = sum(1 for message in ctx.history if message["role"] == "user")
    return Completed([make_artifact("gema", [data_part({"teks": text, "giliran_user": turns})])], "selesai")


class A2ATestCase(unittest.IsolatedAsyncioTestCase):
    def make(self, handler=echo, token="", task_timeout=600):
        self.audit = AuditLog(Path(self.enterContext(tempfile.TemporaryDirectory())) / "audit.jsonl")
        card = agent_card("gema", "Agent uji", "http://uji/", [{"id": "gema", "name": "Gema"}], secured=bool(token))
        self.server = A2AServer(card, handler, audit=self.audit, token=token, task_timeout=task_timeout)
        return self.server

    def client(self, token="") -> A2AClient:
        return A2AClient("http://uji", sender="orchestrator", token=token, audit=self.audit,
                         transport=httpx.ASGITransport(app=self.server.app))


class TestA2AServer(A2ATestCase):
    async def test_agent_card(self):
        self.make()
        async with self.client() as client:
            card = await client.card()
            old = (await client._http.get("/.well-known/agent.json")).json()
        self.assertEqual(card["protocolVersion"], "0.3.0")
        self.assertEqual(card["skills"][0]["id"], "gema")
        self.assertFalse(card["capabilities"]["streaming"])
        self.assertEqual(old, card)

    async def test_siklus_task_non_blocking(self):
        self.make()
        async with self.client() as client:
            task = await client.send([text_part("halo")], correlation_id="c1")
            self.assertEqual(task["status"]["state"], "submitted")
            self.assertEqual(task["kind"], "task")
            done = await client.wait(task["id"], poll_interval=0.01, correlation_id="c1")
        self.assertEqual(done["status"]["state"], "completed")
        self.assertEqual(artifact_data(done, "gema"), {"teks": "halo", "giliran_user": 1})
        self.assertEqual(status_text(done), "selesai")
        states = [e.status for e in self.audit.read("c1") if e.jenis == "status_task"]
        self.assertEqual(states, ["submitted", "working", "completed"])
        sent = [e for e in self.audit.read("c1") if e.jenis == "task_dikirim"][0]
        self.assertEqual((sent.pengirim, sent.penerima), ("orchestrator", "gema"))

    async def test_blocking(self):
        self.make()
        async with self.client() as client:
            task = await client.send([text_part("halo")], blocking=True)
        self.assertEqual(task["status"]["state"], "completed")

    async def test_input_required_multi_turn(self):
        self.make()
        async with self.client() as client:
            task = await client.run([data_part({})])
            self.assertEqual(task["status"]["state"], "input-required")
            self.assertEqual(status_text(task), "Siapa namamu?")
            follow = await client.send([text_part("Budi")], task_id=task["id"])
            done = await client.wait(follow["id"], poll_interval=0.01)
            full = await client.get(task["id"])
        self.assertEqual(done["id"], task["id"])  # task yang sama
        self.assertEqual(artifact_data(done, "gema"), {"teks": "Budi", "giliran_user": 2})
        self.assertEqual([m["role"] for m in full["history"]], ["user", "agent", "user", "agent"])
        self.assertEqual({m["contextId"] for m in full["history"]}, {task["contextId"]})

    async def test_task_terminal_tidak_bisa_dilanjutkan(self):
        self.make()
        async with self.client() as client:
            task = await client.send([text_part("halo")], blocking=True)
            with self.assertRaises(A2AError) as caught:
                await client.send([text_part("lagi")], task_id=task["id"])
            # pekerjaan lanjutan: task baru dalam context yang sama
            follow = await client.send([text_part("lagi")], context_id=task["contextId"], blocking=True,
                                       reference_task_ids=[task["id"]])
        self.assertEqual(caught.exception.code, UNSUPPORTED_OPERATION)
        self.assertNotEqual(follow["id"], task["id"])
        self.assertEqual(follow["contextId"], task["contextId"])
        self.assertEqual(follow["history"][0]["referenceTaskIds"], [task["id"]])

    async def test_failed_crash_dan_timeout(self):
        self.make(task_timeout=0.1)
        async with self.client() as client:
            failed = await client.run([text_part("gagal")])
            crash = await client.run([text_part("crash")])
            slow = await client.run([text_part("lambat")])
        self.assertEqual((failed["status"]["state"], status_text(failed)), ("failed", "tidak bisa"))
        self.assertEqual(crash["status"]["state"], "failed")
        self.assertIn("ValueError: rusak", status_text(crash))
        self.assertIn("timeout", status_text(slow))

    async def test_cancel(self):
        self.make()
        async with self.client() as client:
            task = await client.send([text_part("lambat")])
            await asyncio.sleep(0.01)
            canceled = await client.cancel(task["id"])
            await asyncio.sleep(0.01)
            again = await client.get(task["id"])
        self.assertEqual(canceled["status"]["state"], "canceled")
        self.assertEqual(again["status"]["state"], "canceled")

    async def test_task_paralel(self):
        self.make()
        async with self.client() as client:
            tasks = await asyncio.gather(*[client.run([text_part(f"klaim {i}")]) for i in range(5)])
        self.assertEqual([artifact_data(t, "gema")["teks"] for t in tasks], [f"klaim {i}" for i in range(5)])
        self.assertEqual(len({t["id"] for t in tasks}), 5)

    async def test_error_jsonrpc(self):
        self.make()
        async with self.client() as client:
            with self.assertRaises(A2AError) as unknown:
                await client.get("tidak-ada")
            with self.assertRaises(A2AError) as stream:
                await client._rpc("message/stream", {})
            with self.assertRaises(A2AError) as method:
                await client._rpc("hapus/semua", {})
            with self.assertRaises(A2AError) as params:
                await client._rpc("message/send", {"message": {"parts": []}})
        self.assertEqual(unknown.exception.code, TASK_NOT_FOUND)
        self.assertEqual(stream.exception.code, UNSUPPORTED_OPERATION)
        self.assertEqual(method.exception.code, -32601)
        self.assertEqual(params.exception.code, -32602)

    async def test_token(self):
        self.make(token="rahasia")
        async with self.client() as anonymous:
            card = await anonymous.card()  # Agent Card tetap publik
            with self.assertRaises(A2AError) as caught:
                await anonymous.send([text_part("halo")])
        async with self.client(token="rahasia") as allowed:
            task = await allowed.send([text_part("halo")], blocking=True)
        self.assertEqual(card["security"], [{"bearer": []}])
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(task["status"]["state"], "completed")


class TestWorkerServer(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch("agents.worker.PREFETCH", False))  # skrip LLM palsu mengasumsikan tanpa langkah 0

    async def run_task(self, script, parts):
        llm = FakeLLM(script)
        servers = {"cekfakta": cekfakta.server, "wiki": wiki.server, "fetch": fetch.server}
        async with MCPHub(servers) as hub:
            server = build_server(llm, hub=hub)
            transport = httpx.ASGITransport(app=server.app)
            async with A2AClient("http://worker", token="", transport=transport) as client:
                card = await client.card()
                task = await client.run(parts)
                if task["status"]["state"] == "input-required":
                    task = await client.send([data_part({"klaim": "Vaksin menyebabkan kanker"})], task_id=task["id"])
                    task = await client.wait(task["id"], poll_interval=0.01)
        return card, task

    async def test_klaim_diperiksa_lewat_a2a(self):
        card, task = await self.run_task([SEARCH, answer()], [data_part({"klaim": "Vaksin menyebabkan kanker"})])
        self.assertEqual(card["skills"][0]["id"], "periksa_klaim")
        self.assertEqual(task["status"]["state"], "completed")
        result = artifact_data(task, ARTIFACT)
        self.assertEqual(result["sikap"], "MEMBANTAH")
        self.assertEqual([step["tool"] for step in result["langkah"]], ["cari_cek_fakta", ANSWER_TOOL])
        self.assertIn("MEMBANTAH", status_text(task))

    async def test_klaim_kosong_minta_masukan(self):
        _, task = await self.run_task([SEARCH, answer()], [text_part("  ")])
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(artifact_data(task, ARTIFACT)["klaim"], "Vaksin menyebabkan kanker")

    async def test_llm_gagal_task_failed_dengan_artifact(self):
        _, task = await self.run_task([ToolCall("cari_cek_fakta", {"alasan": "x", "query": "q"})],
                                      [data_part({"klaim": "x"})])
        self.assertEqual(task["status"]["state"], "failed")
        self.assertEqual(artifact_data(task, ARTIFACT)["sikap"], "TIDAK_CUKUP")


if __name__ == "__main__":
    unittest.main()
