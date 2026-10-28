"""Tes Fase 4: ekstraksi klaim, agregasi deterministik, dan delegasi paralel ke Worker lewat A2A (in-memory)."""
import asyncio
import json
import logging
from pathlib import Path
import tempfile
import unittest

import httpx

from agents.common.a2a_client import A2AClient, artifact_data
from agents.common.a2a_server import A2AServer, Completed, agent_card, data_part, make_artifact, text_part
from agents.common.audit import AuditLog
from agents.common.llm import LLMError, LLMResult
from agents.orchestrator import (BUKAN_FAKTUAL, FAKTA, HOAKS, MAX_KLAIM, SEBAGIAN, TIDAK_TERVERIFIKASI, Orchestrator,
                                 aggregate, build_server, extract_claims, format_report)
from agents.worker_server import ARTIFACT as WORKER_ARTIFACT, SKILL as WORKER_SKILL

logging.getLogger("asyncio").setLevel(logging.ERROR)


class FakeExtractor:
    """LLM palsu untuk ekstraksi klaim: mengembalikan daftar klaim yang sudah ditentukan."""

    name = "palsu"

    def __init__(self, claims: list[dict] | None = None, error: Exception | None = None,
                 retry_claims: list[dict] | None = None) -> None:
        self.responses = [claims or []] + ([retry_claims] if retry_claims is not None else [])
        self.error = error
        self.prompts: list[str] = []

    def generate(self, prompt, system="", temperature=0.0, schema=None) -> LLMResult:
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        claims = self.responses[min(len(self.prompts), len(self.responses)) - 1]
        return LLMResult(json.dumps({"klaim": claims}), token=50)


def claim(teks: str, jenis: str = "fakta") -> dict:
    return {"teks": teks, "jenis": jenis, "alasan_jenis": "uji"}


class FakeWorker:
    """Server A2A Worker palsu: sikap ditentukan kata di klaim; mencatat jumlah task yang berjalan bersamaan."""

    def __init__(self, audit: AuditLog) -> None:
        self.active = 0
        self.max_active = 0
        card = agent_card("worker", "palsu", "http://worker/", [WORKER_SKILL])
        self.server = A2AServer(card, self.handle, audit=audit)

    async def handle(self, ctx):
        klaim = ctx.data()["klaim"]
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(5 if "lambat" in klaim else 0.05)
            if "crash" in klaim:
                raise RuntimeError("worker rusak")
        finally:
            self.active -= 1
        sikap = "MEMBANTAH" if "hoaks" in klaim else "TIDAK_CUKUP" if "lokal" in klaim else "MENDUKUNG"
        result = {"klaim": klaim, "sikap": sikap, "keyakinan": 0.9, "ringkasan": f"ringkasan {klaim}",
                  "bukti": [{"url": "https://contoh.id/a", "kutipan": "kutipan"}], "token": 100, "langkah": [],
                  "catatan": []}
        return Completed([make_artifact(WORKER_ARTIFACT, [data_part(result)])], sikap)

    def client(self, audit=None) -> A2AClient:
        return A2AClient("http://worker", sender="orchestrator", token="", audit=audit,
                         transport=httpx.ASGITransport(app=self.server.app))


class TestAggregate(unittest.TestCase):
    def test_aturan(self):
        cases = [
            ([], TIDAK_TERVERIFIKASI),
            ([BUKAN_FAKTUAL, BUKAN_FAKTUAL], TIDAK_TERVERIFIKASI),
            ([FAKTA, FAKTA], FAKTA),
            ([FAKTA, BUKAN_FAKTUAL], FAKTA),
            ([HOAKS], HOAKS),
            ([HOAKS, TIDAK_TERVERIFIKASI, TIDAK_TERVERIFIKASI], HOAKS),
            ([HOAKS, FAKTA], SEBAGIAN),
            ([HOAKS, FAKTA, TIDAK_TERVERIFIKASI], SEBAGIAN),
            ([FAKTA, TIDAK_TERVERIFIKASI], TIDAK_TERVERIFIKASI),
            ([TIDAK_TERVERIFIKASI], TIDAK_TERVERIFIKASI),
        ]
        for putusan, expected in cases:
            with self.subTest(putusan=putusan):
                self.assertEqual(aggregate(putusan)[0], expected)

    def test_alasan_menyebut_aturan(self):
        self.assertTrue(aggregate([HOAKS, FAKTA])[1].startswith("aturan 2"))


class TestExtract(unittest.TestCase):
    def test_id_jenis_dan_duplikat(self):
        llm = FakeExtractor([claim("Gempa  terjadi kemarin"), claim("gempa terjadi kemarin"),
                             claim("Pemerintah sangat jahat", "opini"), claim("Harga beras naik", "aneh")])
        claims, token, notes = extract_claims(llm, "pesan")
        self.assertEqual([(c.id, c.teks, c.jenis) for c in claims],
                         [("K1", "Gempa terjadi kemarin", "fakta"), ("K2", "Pemerintah sangat jahat", "opini"),
                          ("K3", "Harga beras naik", "fakta")])
        self.assertEqual((token, notes, len(llm.prompts)), (50, [], 1))

    def test_teks_klaim_dibersihkan(self):
        from agents.orchestrator import clean_claim
        self.assertEqual(clean_claim("الBMKG adalah  lembaga #pemerintah"), "BMKG adalah lembaga pemerintah")
        self.assertEqual(clean_claim("Gempa 릻di Cianjur"), "Gempa di Cianjur")
        self.assertEqual(clean_claim("Kopi tubruk & café, 15% lebih mahal"), "Kopi tubruk & café, 15% lebih mahal")

    def test_maksimal_klaim(self):
        claims, _, _ = extract_claims(FakeExtractor([claim(f"klaim nomor {i}") for i in range(9)]), "pesan")
        self.assertEqual(len(claims), MAX_KLAIM)

    def test_klaim_rusak_memicu_ekstraksi_ulang(self):
        llm = FakeExtractor([claim("Jakarta di Sumatra sekarang"), claim("P", "prediksi")],
                            retry_claims=[claim("Jakarta di Sumatra sekarang"), claim("Tahun depan gempa besar", "prediksi")])
        claims, token, notes = extract_claims(llm, "pesan")
        self.assertEqual([c.teks for c in claims], ["Jakarta di Sumatra sekarang", "Tahun depan gempa besar"])
        self.assertEqual((len(llm.prompts), token), (2, 100))
        self.assertIn("rusak", notes[0])
        self.assertIn("['P']", llm.prompts[1])  # umpan balik ada di prompt ulang

    def test_klaim_tetap_rusak_dibuang(self):
        llm = FakeExtractor([claim("Jakarta di Sumatra sekarang"), claim("bantahan " * 8)])
        claims, _, notes = extract_claims(llm, "pesan")
        self.assertEqual([c.teks for c in claims], ["Jakarta di Sumatra sekarang"])
        self.assertIn("dibuang", notes[-1])


class TestOrchestrator(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.audit = AuditLog(Path(self.enterContext(tempfile.TemporaryDirectory())) / "audit.jsonl")
        self.worker = FakeWorker(self.audit)

    async def check(self, claims, client=None, **kwargs):
        orchestrator = Orchestrator(FakeExtractor(claims), client or self.worker.client(self.audit), self.audit,
                                    poll_interval=0.01, **kwargs)
        return await orchestrator.check_message("pesan uji", correlation_id="c1")

    async def test_pesan_campuran_diperiksa_paralel(self):
        report = await self.check([claim("gempa terjadi kemarin"), claim("vaksin itu hoaks"), claim("ayo sebarkan semua", "opini"),
                                   claim("sembako lokal dibagikan")])
        self.assertEqual([c["putusan"] for c in report["klaim"]], [FAKTA, HOAKS, BUKAN_FAKTUAL, TIDAK_TERVERIFIKASI])
        self.assertEqual(report["putusan"], SEBAGIAN)
        self.assertEqual(self.worker.max_active, 3)  # tiga klaim faktual dikerjakan bersamaan
        self.assertEqual(report["token"], {"orchestrator": 50, "worker": 300, "critic": 0})
        self.assertNotIn("worker_task_id", report["klaim"][2])  # opini tidak dikirim ke Worker
        self.assertEqual(len({c["worker_task_id"] for c in report["klaim"] if "worker_task_id" in c}), 3)

    async def test_correlation_id_sampai_ke_worker(self):
        await self.check([claim("gempa terjadi kemarin")])
        worker_states = [e.status for e in self.audit.read("c1") if e.jenis == "status_task"]
        self.assertEqual(worker_states, ["submitted", "working", "completed"])
        jenis = [e.jenis for e in self.audit.read("c1")]
        self.assertEqual((jenis[0], jenis[-1]), ("llm_call", "task_selesai"))

    async def test_worker_gagal_dan_timeout_jadi_tidak_terverifikasi(self):
        report = await self.check([claim("gempa terjadi kemarin"), claim("worker crash sekarang"), claim("proses lambat sekali")], claim_timeout=0.3)
        self.assertEqual([c["putusan"] for c in report["klaim"]], [FAKTA, TIDAK_TERVERIFIKASI, TIDAK_TERVERIFIKASI])
        self.assertEqual([c["status_task"] for c in report["klaim"]], ["completed", "failed", "canceled"])
        self.assertIn("RuntimeError", report["klaim"][1]["catatan"][-1])
        self.assertEqual(report["putusan"], TIDAK_TERVERIFIKASI)

    async def test_worker_mati(self):
        def refuse(request):
            raise httpx.ConnectError("connection refused")
        dead = A2AClient("http://worker", token="", transport=httpx.MockTransport(refuse))
        report = await self.check([claim("gempa terjadi kemarin")], client=dead)
        self.assertEqual(report["klaim"][0]["status_task"], "tidak_terkirim")
        self.assertEqual(report["putusan"], TIDAK_TERVERIFIKASI)

    async def test_ekstraksi_gagal(self):
        orchestrator = Orchestrator(FakeExtractor(error=LLMError("kuota habis")), self.worker.client(), self.audit)
        report = await orchestrator.check_message("pesan")
        self.assertEqual((report["putusan"], report["klaim"]), (TIDAK_TERVERIFIKASI, []))
        self.assertIn("kuota habis", report["catatan"][0])

    async def test_format_report(self):
        text = format_report(await self.check([claim("vaksin itu hoaks"), claim("sebarkan ke semua", "opini")]))
        self.assertIn("PUTUSAN: HOAKS", text)
        self.assertIn("[K2] BUKAN KLAIM FAKTUAL", text)


class TestOrchestratorServer(unittest.IsolatedAsyncioTestCase):
    async def test_pesan_lewat_a2a(self):
        audit = AuditLog(Path(self.enterContext(tempfile.TemporaryDirectory())) / "audit.jsonl")
        worker = FakeWorker(audit)
        server = build_server(FakeExtractor([claim("vaksin itu hoaks")]), worker_client=worker.client(), audit=audit,
                              use_critic=False, poll_interval=0.01)
        async with server.app.router.lifespan_context(server.app):  # lifespan: discovery Agent Card Worker
            async with A2AClient("http://orch", token="", transport=httpx.ASGITransport(app=server.app)) as client:
                card = await client.card()
                empty = await client.run([text_part("")], poll_interval=0.01)
                task = await client.run([text_part("Vaksin itu hoaks, sebarkan!")], correlation_id="c9",
                                        poll_interval=0.01)
        self.assertEqual(card["skills"][0]["id"], "periksa_pesan")
        self.assertEqual(empty["status"]["state"], "input-required")
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(artifact_data(task, "laporan")["putusan"], HOAKS)
        self.assertEqual(artifact_data(task, "laporan")["correlation_id"], "c9")
        text = [a for a in task["artifacts"] if a["name"] == "laporan_teks"][0]["parts"][0]["text"]
        self.assertIn("PUTUSAN: HOAKS", text)

    async def test_worker_tanpa_skill_ditolak_saat_mulai(self):
        bad = A2AServer(agent_card("lain", "x", "http://lain/", [{"id": "lain", "name": "Lain"}]), None)
        client = A2AClient("http://lain", token="", transport=httpx.ASGITransport(app=bad.app))
        server = build_server(FakeExtractor(), worker_client=client, use_critic=False)
        with self.assertRaisesRegex(RuntimeError, "periksa_klaim"):
            async with server.app.router.lifespan_context(server.app):
                pass


if __name__ == "__main__":
    unittest.main()
