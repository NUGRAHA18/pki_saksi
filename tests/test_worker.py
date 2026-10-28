"""Tes Fase 2: loop ReAct Worker dengan LLM palsu (skrip tool call) dan server MCP asli in-memory + HTTP palsu."""
from pathlib import Path
import tempfile
import unittest

from agents.common.audit import AuditLog
from agents.common.llm import ChatResult, LLMError, ToolCall
from agents.common.mcp_client import MCPHub
from agents.worker import ANSWER_TOOL, Worker, tool_declarations
from mcp_servers import cekfakta, fetch, wiki
from tests.test_mcp_servers import ServerTestCase

TEMPO = "https://cekfakta.tempo.co/a"  # URL yang muncul di hasil cekfakta palsu (FACTCHECK_RESPONSE)


class FakeLLM:
    """Mengembalikan tool call sesuai skrip dan mencatat tool apa saja yang ditawarkan di tiap giliran."""

    name = "palsu"

    def __init__(self, script: list[ToolCall]) -> None:
        self.script = list(script)
        self.offered: list[list[str]] = []
        self.messages: list[list[dict]] = []

    def chat(self, messages, system, tools) -> ChatResult:
        self.offered.append([tool["name"] for tool in tools])
        self.messages.append(list(messages))
        if not self.script:
            raise LLMError("skrip habis")
        return ChatResult(self.script.pop(0), token=100)


def answer(sikap="MEMBANTAH", url=TEMPO, kutipan="Keliru", keyakinan=0.9) -> ToolCall:
    bukti = [{"url": url, "kutipan": kutipan}] if url else []
    return ToolCall(ANSWER_TOOL, {"alasan": "cukup", "sikap": sikap, "bukti": bukti, "keyakinan": keyakinan,
                                  "ringkasan": "Klaim ini sudah dibantah."})


SEARCH = ToolCall("cari_cek_fakta", {"alasan": "cek dulu arsip cek fakta", "query": "vaksin kanker"})


class TestWorker(ServerTestCase):
    async def run_worker(self, script, **kwargs):
        llm = FakeLLM(script)
        servers = {"cekfakta": cekfakta.server, "wiki": wiki.server, "fetch": fetch.server}
        async with MCPHub(servers, agent="worker", audit=kwargs.pop("audit", None)) as hub:
            kwargs.setdefault("prefetch", False)  # tes loop ReAct; pencarian awal diuji terpisah
            result = await Worker(llm, hub, audit=hub.audit, **kwargs).check("Vaksin menyebabkan kanker",
                                                                             correlation_id="c1")
        return result, llm

    def test_deklarasi_tool_diberi_parameter_alasan(self):
        class Tool:
            name, description = "cari", "Cari."
            input_schema = {"type": "object", "title": "x", "properties": {"q": {"type": "string"}}, "required": ["q"]}
        declaration = tool_declarations([Tool()])[0]
        self.assertEqual(declaration["parameters"]["required"], ["alasan", "q"])
        self.assertNotIn("title", declaration["parameters"])
        self.assertEqual(Tool.input_schema["required"], ["q"])  # skema asli tidak ikut berubah

    async def test_alur_react_sampai_jawab(self):
        audit = AuditLog(Path(self.enterContext(tempfile.TemporaryDirectory())) / "a.jsonl")
        result, llm = await self.run_worker([SEARCH, answer()], audit=audit)
        self.assertEqual((result.sikap, result.status, result.keyakinan), ("MEMBANTAH", "selesai", 0.9))
        self.assertEqual([step.tool for step in result.langkah], ["cari_cek_fakta", ANSWER_TOOL])
        self.assertEqual(result.langkah[0].alasan, "cek dulu arsip cek fakta")
        self.assertNotIn("alasan", result.langkah[0].argumen)  # alasan tidak dikirim ke server MCP
        self.assertIn("Tempo", result.langkah[0].observasi_lengkap)  # hasil tool asli tersimpan untuk Critic
        self.assertEqual(result.token, 200)
        # Observation dikirim balik ke model sebagai function response
        self.assertEqual(llm.messages[1][-1]["role"], "tool")
        self.assertIn("Tempo", llm.messages[1][-1]["result"]["hasil"])
        jenis = [event.jenis for event in audit.read("c1")]
        self.assertEqual(jenis, ["llm_call", "tool_call", "llm_call", "task_selesai"])

    async def test_url_karangan_ditolak_lalu_diperbaiki(self):
        result, llm = await self.run_worker([SEARCH, answer(url="https://hoaks.id/karangan"), answer()])
        self.assertEqual(result.sikap, "MEMBANTAH")
        self.assertEqual(result.bukti[0]["url"], TEMPO)
        self.assertIn("tidak pernah muncul", result.catatan[0])
        self.assertIn("tidak pernah muncul", llm.messages[2][-1]["result"]["error"])

    async def test_sikap_tanpa_bukti_ditolak(self):
        result, _ = await self.run_worker([answer(url=None), answer(sikap="TIDAK_CUKUP", url=None, keyakinan=0.2)])
        self.assertEqual(result.sikap, "TIDAK_CUKUP")
        self.assertIn("wajib disertai bukti", result.catatan[0])

    async def test_ringkasan_macet_ditolak(self):
        broken = answer()
        broken.args["ringkasan"] = "Klaim telah dibantah " + "bantahan " * 20 + "l l l l l l l l"
        result, llm = await self.run_worker([SEARCH, broken, answer()])
        self.assertEqual(result.ringkasan, "Klaim ini sudah dibantah.")
        self.assertIn("rusak", llm.messages[2][-1]["result"]["error"])

    def test_deteksi_teks_rusak_tidak_salah_tangkap(self):
        from agents.worker import _degenerate
        self.assertFalse(_degenerate("Gempa M5,9 terjadi pada 4 Oktober 2026 dan tidak berpotensi tsunami."))
        self.assertFalse(_degenerate("ha ha ha"))
        self.assertTrue(_degenerate("kankernya kankernya kankernya kankernya kankernya kankernya"))

    async def test_panggilan_berulang_tidak_dijalankan(self):
        result, _ = await self.run_worker([SEARCH, SEARCH, answer()])
        self.assertEqual(len(self.http.requests), 1)
        self.assertIn("persis sama", result.langkah[1].observasi)
        self.assertIn("panggilan berulang", result.catatan[0])

    async def test_batas_langkah_hanya_menawarkan_jawab(self):
        searches = [ToolCall("cari_wikipedia", {"alasan": "cari", "query": f"q{i}"}) for i in range(2)]
        result, llm = await self.run_worker(searches + [answer(sikap="TIDAK_CUKUP", url=None)], max_steps=2)
        self.assertEqual(result.status, "batas_langkah")
        self.assertEqual(llm.offered[-1], [ANSWER_TOOL])
        self.assertGreater(len(llm.offered[0]), 1)

    async def test_error_tool_menjadi_observasi(self):
        bad = ToolCall("ringkasan_wikipedia", {"alasan": "baca", "judul": "x", "bahasa": "jp"})
        result, llm = await self.run_worker([bad, answer(sikap="TIDAK_CUKUP", url=None)])
        self.assertTrue(result.langkah[0].observasi.startswith("ERROR"))
        self.assertIn("bahasa harus", llm.messages[1][-1]["result"]["hasil"])
        self.assertEqual(result.status, "selesai")

    async def test_llm_gagal_tidak_crash(self):
        result, _ = await self.run_worker([SEARCH])  # skrip habis -> LLMError di langkah 2
        self.assertEqual((result.sikap, result.status), ("TIDAK_CUKUP", "error"))
        self.assertIn("LLM gagal", result.catatan[0])

    async def test_pencarian_awal_paralel(self):
        result, llm = await self.run_worker([answer()], prefetch=True)
        self.assertEqual([(s.nomor, s.tool) for s in result.langkah[:2]], [(0, "cari_cek_fakta"), (0, "cari_wikipedia")])
        first_turn = llm.messages[0]
        self.assertIn("Hasil pencarian awal", first_turn[1]["text"])
        self.assertIn("Tempo", first_turn[1]["text"])
        self.assertEqual(result.sikap, "MEMBANTAH")  # URL dari pencarian awal sah sebagai bukti
        self.assertEqual(result.token, 100)  # satu panggilan model saja
        self.assertIn("Tempo", result.langkah[0].observasi_lengkap)  # bahan verifikasi Critic

    async def test_pencarian_awal_tidak_diulang_model(self):
        result, _ = await self.run_worker([ToolCall("cari_cek_fakta", {"alasan": "cek", "query": "Vaksin menyebabkan kanker"}),
                                           answer()], prefetch=True)
        self.assertIn("persis sama", result.langkah[2].observasi)
        self.assertEqual(len(self.http.requests), 2)  # hanya dua permintaan pencarian awal

    async def test_riwayat_lama_diringkas(self):
        searches = [ToolCall("cari_wikipedia", {"alasan": "cari", "query": f"q{i}"}) for i in range(3)]
        _, llm = await self.run_worker([SEARCH, *searches, answer(sikap="TIDAK_CUKUP", url=None)])
        last_view = llm.messages[-1]
        tool_results = [m["result"]["hasil"] for m in last_view if m["role"] == "tool"]
        self.assertEqual(len(tool_results), 4)
        self.assertIn("[diringkas]", tool_results[0])  # hasil cek fakta (paling lama) diringkas ...
        self.assertIn(TEMPO, tool_results[0])  # ... tetapi URL-nya tetap ada
        self.assertNotIn("[diringkas]", tool_results[-1])
        self.assertNotIn("[diringkas]", tool_results[-2])

    async def test_url_wikipedia_dengan_koma_dianggap_sama(self):
        search = ToolCall("cari_wikipedia", {"alasan": "cari", "query": "BMKG"})
        url = "https://id.wikipedia.org/wiki/Badan_Meteorologi"
        result, _ = await self.run_worker([search, answer(sikap="MENDUKUNG", url=url + "/")])
        self.assertEqual(result.sikap, "MENDUKUNG")
        self.assertEqual(result.catatan, [])


if __name__ == "__main__":
    unittest.main()
