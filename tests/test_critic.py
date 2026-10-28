"""Tes Fase 5: verifikasi kutipan Critic, aturan keras, LLM-as-judge, server Critic, dan putaran revisi Orchestrator."""
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from agents.common.a2a_client import A2AClient, artifact_data
from agents.common.a2a_server import A2AServer, Completed, Failed, agent_card, data_part, make_artifact
from agents.common.audit import AuditLog
from agents.common.llm import LLMError, LLMResult
from agents.common.mcp_client import MCPHub
from agents.critic import (ARTIFACT, DI_HALAMAN, DI_HASIL_TOOL, SKILL as CRITIC_SKILL, TIDAK_BISA_DICEK,
                           TIDAK_DITEMUKAN, Critic, build_server, quote_in_tool_outputs)
from agents.orchestrator import FAKTA, HOAKS, MAX_REVISI, TIDAK_TERVERIFIKASI, Orchestrator
from agents.worker_server import ARTIFACT as WORKER_ARTIFACT, SKILL as WORKER_SKILL
from mcp_servers import fetch
from tests.test_mcp_servers import ServerTestCase
from tests.test_orchestrator import FakeExtractor, claim

logging.getLogger("asyncio").setLevel(logging.ERROR)

PAGES = {
    "https://berita.id/gempa": ("Gempa", "BMKG mencatat gempa M5,9 di Sumba Barat Daya pada 4 Oktober 2026. Isi lain."),
    "https://wiki.id/Jakarta": ("Jakarta", "Jakarta adalah ibu kota dan provinsi di Pulau Jawa, Indonesia."),
}
TEMPO = "https://www.tempo.co/cekfakta/keliru-kanker-turbo"  # halaman menolak bot (403)


def fake_fetch_page(url):
    if url in PAGES:
        return PAGES[url][0], PAGES[url][1], []
    raise ValueError("halaman membalas HTTP 403")


def hasil(sikap, bukti, langkah=None) -> dict:
    return {"sikap": sikap, "keyakinan": 0.9, "ringkasan": "ringkasan", "langkah": langkah or [],
            "bukti": [{"url": url, "kutipan": kutipan} for url, kutipan in bukti]}


# Observation cari_cek_fakta seperti yang dicatat kode Worker (JSON hasil tool)
CEKFAKTA_STEP = {"tool": "cari_cek_fakta", "argumen": {"query": "kanker turbo"}, "observasi": "1 hasil",
                 "observasi_lengkap": json.dumps({"hasil": [{"judul": "Keliru, Vaksin COVID-19 Menyebabkan Kanker Turbo",
                                                             "rating": "Keliru", "url": TEMPO}]}, ensure_ascii=False)}


class FakeJudge:
    name = "palsu"

    def __init__(self, keputusan="TERIMA", error: Exception | None = None) -> None:
        self.keputusan, self.error, self.prompts = keputusan, error, []

    def generate(self, prompt, system="", temperature=0.0, schema=None) -> LLMResult:
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return LLMResult(json.dumps({"keputusan": self.keputusan, "alasan": f"dinilai {self.keputusan}",
                                     "saran_perbaikan": "cari sumber lain"}), token=70)


class CriticTestCase(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(fetch, "fetch_page", side_effect=fake_fetch_page))

    async def review(self, result, judge=None):
        judge = judge or FakeJudge()
        async with MCPHub({"fetch": fetch.server}) as hub:
            verdict = await Critic(judge, hub).review("klaim uji", result)
        return verdict, judge


class TestVerifikasiKutipan(CriticTestCase):
    async def test_status_kutipan(self):
        result = hasil("MEMBANTAH", [
            ("https://wiki.id/Jakarta", "ibu kota dan provinsi di Pulau Jawa"),            # ada di halaman
            (TEMPO, "Keliru, Vaksin COVID-19 Menyebabkan Kanker Turbo"),                   # 403, ada di hasil tool
        ], [CEKFAKTA_STEP])
        verdict, judge = await self.review(result)
        statuses = [check["status"] for check in verdict.pemeriksaan_kutipan]
        self.assertEqual(statuses, [DI_HALAMAN, DI_HASIL_TOOL])
        self.assertEqual((verdict.keputusan, verdict.sumber_keputusan), ("TERIMA", "llm"))
        self.assertIn("ditemukan_di_hasil_tool", judge.prompts[0])  # status verifikasi sampai ke LLM

    async def test_kutipan_karangan_ditolak_tanpa_llm(self):
        result = hasil("MENDUKUNG", [("https://berita.id/gempa", "BMKG memastikan tsunami setinggi 10 meter")])
        verdict, judge = await self.review(result)
        self.assertEqual(verdict.pemeriksaan_kutipan[0]["status"], TIDAK_DITEMUKAN)
        self.assertEqual((verdict.keputusan, verdict.sumber_keputusan), ("TOLAK", "aturan"))
        self.assertIn("tidak ditemukan", verdict.alasan)
        self.assertEqual(judge.prompts, [])

    async def test_halaman_tak_terbaca_dan_tidak_ada_di_hasil_tool(self):
        result = hasil("MEMBANTAH", [(TEMPO, "Keliru, Vaksin COVID-19 Menyebabkan Kanker Turbo")])
        verdict, judge = await self.review(result)
        self.assertEqual(verdict.pemeriksaan_kutipan[0]["status"], TIDAK_BISA_DICEK)
        self.assertEqual((verdict.keputusan, verdict.sumber_keputusan), ("TOLAK", "aturan"))
        self.assertIn("tanpa satu pun kutipan", verdict.alasan)

    async def test_tidak_cukup_dinilai_llm(self):
        verdict, judge = await self.review(hasil("TIDAK_CUKUP", []), FakeJudge("TOLAK"))
        self.assertEqual((verdict.keputusan, verdict.saran_perbaikan, verdict.token), ("TOLAK", "cari sumber lain", 70))

    async def test_llm_gagal_bukan_berarti_terima(self):
        result = hasil("MENDUKUNG", [("https://berita.id/gempa", "gempa M5,9 di Sumba Barat Daya")])
        with self.assertRaisesRegex(RuntimeError, "penilaian LLM gagal"):
            await self.review(result, FakeJudge(error=LLMError("kuota habis")))

    def test_alasan_macet_dipotong(self):
        from agents.worker import trim_degenerate
        text = "Pemeriksa sudah mencari dengan kata kunci yang bicalan, " + "kanlah-" * 12
        self.assertEqual(trim_degenerate(text), "Pemeriksa sudah mencari dengan kata kunci yang bicalan …")
        self.assertEqual(trim_degenerate("Alasan normal."), "Alasan normal.")

    def test_kutipan_di_hasil_tool_harus_untuk_url_yang_sama(self):
        self.assertTrue(quote_in_tool_outputs(TEMPO, "keliru vaksin covid 19", [CEKFAKTA_STEP]))
        self.assertFalse(quote_in_tool_outputs("https://lain.id/x", "keliru vaksin covid 19", [CEKFAKTA_STEP]))


class TestCriticServer(CriticTestCase):
    async def test_penilaian_lewat_a2a(self):
        async with MCPHub({"fetch": fetch.server}) as hub:
            server = build_server(FakeJudge("TERIMA"), hub=hub)
            transport = httpx.ASGITransport(app=server.app)
            async with A2AClient("http://critic", token="", transport=transport) as client:
                card = await client.card()
                empty = await client.run([data_part({})], poll_interval=0.01)
                task = await client.run([data_part({"klaim": "Jakarta di Jawa", "hasil_worker": hasil(
                    "MENDUKUNG", [("https://wiki.id/Jakarta", "provinsi di Pulau Jawa")])})], poll_interval=0.01)
        self.assertEqual(card["skills"][0]["id"], "nilai_hasil")
        self.assertEqual(empty["status"]["state"], "input-required")
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(artifact_data(task, ARTIFACT)["keputusan"], "TERIMA")

    async def test_llm_gagal_task_failed(self):
        async with MCPHub({"fetch": fetch.server}) as hub:
            server = build_server(FakeJudge(error=LLMError("500")), hub=hub)
            async with A2AClient("http://critic", token="", transport=httpx.ASGITransport(app=server.app)) as client:
                task = await client.run([data_part({"klaim": "Jakarta di Jawa", "hasil_worker": hasil(
                    "MENDUKUNG", [("https://wiki.id/Jakarta", "provinsi di Pulau Jawa")])})], poll_interval=0.01)
        self.assertEqual(task["status"]["state"], "failed")


# --- putaran revisi di Orchestrator ----------------------------------------------------------------------------------

class RecordingWorker:
    """Worker palsu: sikap ke-n dari daftar; mencatat setiap pesan yang diterima (feedback, contextId, referensi)."""

    def __init__(self, sikap_list: list[str]) -> None:
        self.sikap_list, self.messages = list(sikap_list), []
        self.server = A2AServer(agent_card("worker", "palsu", "http://worker/", [WORKER_SKILL]), self.handle)

    async def handle(self, ctx):
        self.messages.append(ctx.message)
        sikap = self.sikap_list[min(len(self.messages), len(self.sikap_list)) - 1]
        result = {**hasil(sikap, [("https://wiki.id/Jakarta", f"kutipan {len(self.messages)}")]), "token": 100,
                  "catatan": []}
        return Completed([make_artifact(WORKER_ARTIFACT, [data_part(result)])])


class ScriptedCritic:
    def __init__(self, keputusan_list: list[str], fail: bool = False) -> None:
        self.keputusan_list, self.fail, self.calls = list(keputusan_list), fail, 0
        self.server = A2AServer(agent_card("critic", "palsu", "http://critic/", [CRITIC_SKILL]), self.handle)

    async def handle(self, ctx):
        self.calls += 1
        if self.fail:
            return Failed("penilaian LLM gagal: 500")
        keputusan = self.keputusan_list[min(self.calls, len(self.keputusan_list)) - 1]
        verdict = {"keputusan": keputusan, "alasan": f"alasan {self.calls}", "saran_perbaikan": f"saran {self.calls}",
                   "sumber_keputusan": "llm", "token": 70, "pemeriksaan_kutipan": []}
        return Completed([make_artifact(ARTIFACT, [data_part(verdict)])])


def client_for(server: A2AServer, name: str) -> A2AClient:
    return A2AClient(f"http://{name}", token="", transport=httpx.ASGITransport(app=server.app))


class TestRevisi(unittest.IsolatedAsyncioTestCase):
    async def check(self, worker, critic=None, critic_client=None):
        audit = AuditLog(Path(self.enterContext(tempfile.TemporaryDirectory())) / "audit.jsonl")
        orchestrator = Orchestrator(FakeExtractor([claim("Jakarta terletak di Sumatra")]),
                                    client_for(worker.server, "worker"), audit, poll_interval=0.01,
                                    critic=critic_client or client_for(critic.server, "critic"))
        report = await orchestrator.check_message("pesan", correlation_id="c1")
        return report, report["klaim"][0], audit

    async def test_tolak_lalu_revisi_diterima(self):
        worker = RecordingWorker(["MENDUKUNG", "MEMBANTAH"])
        report, entry, audit = await self.check(worker, ScriptedCritic(["TOLAK", "TERIMA"]))
        self.assertEqual((entry["putusan"], entry["revisi"]), (HOAKS, 1))
        self.assertEqual([(p["sikap"], p["critic"]) for p in entry["putaran"]], [("MENDUKUNG", "TOLAK"),
                                                                              ("MEMBANTAH", "TERIMA")])
        first, second = worker.messages
        # revisi = task baru dalam context yang sama, merujuk task yang ditolak, membawa umpan balik Critic
        self.assertNotEqual(first["taskId"], second["taskId"])
        self.assertEqual(second["contextId"], first["contextId"])
        self.assertEqual(second["referenceTaskIds"], [first["taskId"]])
        feedback = second["parts"][0]["data"]["feedback"]
        self.assertIn("alasan 1", feedback)
        self.assertIn("saran 1", feedback)
        self.assertIn("MENDUKUNG", feedback)
        self.assertNotIn("feedback", first["parts"][0]["data"])
        self.assertEqual(report["token"], {"orchestrator": 50, "worker": 200, "critic": 140})
        self.assertEqual([e.status for e in audit.read("c1") if e.jenis == "revisi"], ["revisi 1"])

    async def test_ditolak_terus_jadi_tidak_terverifikasi(self):
        worker = RecordingWorker(["MENDUKUNG"])
        critic = ScriptedCritic(["TOLAK"])
        _, entry, _ = await self.check(worker, critic)
        self.assertEqual(len(worker.messages), MAX_REVISI + 1)
        self.assertEqual(critic.calls, MAX_REVISI + 1)
        self.assertEqual((entry["putusan"], entry["revisi"]), (TIDAK_TERVERIFIKASI, MAX_REVISI))
        self.assertIn("ditolak Critic 3 kali", entry["catatan"][-1])

    async def test_langsung_diterima(self):
        _, entry, _ = await self.check(RecordingWorker(["MENDUKUNG"]), ScriptedCritic(["TERIMA"]))
        self.assertEqual((entry["putusan"], entry["revisi"], len(entry["putaran"])), (FAKTA, 0, 1))

    async def test_critic_gagal_atau_mati(self):
        _, entry, _ = await self.check(RecordingWorker(["MENDUKUNG"]), ScriptedCritic([], fail=True))
        self.assertEqual(entry["putusan"], TIDAK_TERVERIFIKASI)
        self.assertIn("Critic gagal menilai", entry["catatan"][-1])

        def refuse(request):
            raise httpx.ConnectError("connection refused")
        dead = A2AClient("http://critic", token="", transport=httpx.MockTransport(refuse))
        _, entry, _ = await self.check(RecordingWorker(["MENDUKUNG"]), critic_client=dead)
        self.assertEqual(entry["putusan"], TIDAK_TERVERIFIKASI)
        self.assertIn("tidak bisa dihubungi", entry["catatan"][-1])


if __name__ == "__main__":
    unittest.main()
