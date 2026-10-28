"""Tes Fase 9: teks kemajuan dari audit log dan tampilan halaman Streamlit (AppTest, tanpa browser/agent)."""
import json
import re
import unittest
from unittest import mock

import httpx

from streamlit.testing.v1 import AppTest

from agents.common.audit import AuditEvent
from agents.common.config import ROOT

import app


def event(sender, receiver, jenis, second, **kwargs) -> AuditEvent:
    return AuditEvent("c1", sender, receiver, jenis, waktu=f"2026-10-28T10:00:{second:02d}.000+00:00", **kwargs)


REPORT = {
    "correlation_id": "uji-tampilan", "pesan": "Awas! Denda tilang naik 150 persen. Pemerintah memang jahat.",
    "putusan": "HOAKS", "alasan_putusan": "aturan 3: 1 klaim hoaks, tidak ada klaim yang terbukti benar",
    "waktu_detik": 120.0, "catatan": [],
    "klaim": [
        {"id": "K1", "teks": "Denda tilang naik 150 persen", "jenis": "fakta", "putusan": "HOAKS",
         "ringkasan": "Korlantas menyatakan belum ada perubahan denda.", "revisi": 1,
         "bukti": [{"url": "https://turnbackhoax.id/articles/1", "kutipan": "[SALAH] Denda Tilang Naik 150 Persen",
                    "sumber": "TurnBackHoax"}],
         "penilaian_critic": {"pemeriksaan_kutipan": [{"url": "https://turnbackhoax.id/articles/1",
                                                       "status": "ditemukan_di_hasil_tool"}]},
         "putaran": [{"putaran": 1, "sikap": "TIDAK_CUKUP", "critic": "TOLAK", "alasan_critic": "pencarian dangkal"},
                     {"putaran": 2, "sikap": "MEMBANTAH", "critic": "TERIMA", "alasan_critic": "bukti cukup"}],
         "langkah": [{"nomor": 1, "alasan": "cek arsip dulu", "tool": "cari_cek_fakta",
                      "argumen": {"query": "denda tilang 150"}, "observasi": "1 hasil"}],
         "catatan": []},
        {"id": "K2", "teks": "Pemerintah memang jahat", "jenis": "opini", "putusan": "BUKAN KLAIM FAKTUAL"},
    ],
}


class TestTeksKemajuan(unittest.TestCase):
    def test_kejadian_menjadi_kalimat(self):
        claims = json.dumps([{"teks": "a", "jenis": "fakta"}, {"teks": "b", "jenis": "opini"}])
        lines = app.progress_lines([
            event("orchestrator", "llm", "llm_call", 0, isi=f"ekstraksi klaim: {claims}"),
            event("orchestrator", "worker", "task_dikirim", 1),
            event("worker", "mcp-berita", "tool_call", 3, isi='cari_berita {"query": "denda tilang"}', status="ok"),
            event("worker", "mcp-fetch", "tool_call", 4, isi='ambil_halaman {"url": "https://www.detik.com/a"}',
                  status="error"),
            event("worker", "llm", "llm_call", 9, percobaan=3, tunggu_detik=15.0),
            event("critic", "critic", "penilaian", 12, status="TOLAK", isi="[llm] bukti membahas klaim lain"),
            event("worker", "worker", "status_task", 13, status="working"),  # tidak ditampilkan
        ])
        texts = [text for _, text in lines]
        self.assertEqual(texts[0], "Pesan dipecah menjadi 2 klaim; 1 klaim faktual akan diperiksa.")
        self.assertIn("Pemeriksa mencari berita: denda tilang", texts)
        self.assertIn("Pemeriksa membaca halaman: detik.com (gagal, mencoba sumber lain)", texts)
        self.assertIn("Menunggu antrean layanan (15 detik).", texts)
        self.assertIn("Pemeriksa independen menolak hasil: bukti membahas klaim lain", texts)
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[-1][0], 12.0)

    def test_label_pemeriksaan_sebelumnya(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "laporan_abc.json"
            path.write_text(json.dumps({"pesan": "Waspada!  Vaksin mRNA Covid-19 terbukti menyebabkan kanker turbo, "
                                                 "sudah banyak korbannya", "putusan": "HOAKS"}), encoding="utf-8")
            label = app.history_label(path)
            path.write_text("{rusak", encoding="utf-8")
            broken = app.history_label(path)
        self.assertRegex(label, r"^\d{1,2} \w{3} \d{2}:\d{2} · HOAKS · Waspada! Vaksin mRNA Covid-19 terbukti menyebabk…$")
        self.assertIn("laporan rusak", broken)

    def test_alasan_tanpa_nomor_aturan(self):
        self.assertEqual(app.plain_reason("aturan 2: 1 klaim hoaks dan 1 klaim fakta"), "1 klaim hoaks dan 1 klaim fakta")


class TestHalaman(unittest.TestCase):
    def run_app(self, chat=None, prompt=None) -> AppTest:
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        if chat is not None:
            at.session_state["chat"] = chat
        # agent dianggap mati, apa pun keadaan komputer saat tes (bisa saja run_all.py sedang berjalan)
        with mock.patch("httpx.get", side_effect=httpx.ConnectError("mati")):
            at.run()
            if prompt is not None:
                at.chat_input[0].set_value(prompt).run()
        return at

    def html(self, at: AppTest) -> str:
        return "\n".join(block.value for block in at.markdown)

    def test_sambutan_dan_contoh_pesan(self):
        at = self.run_app()
        self.assertFalse(at.exception)
        self.assertIn("Tempel pesan berantai", self.html(at))
        examples = [b for b in at.button if b.key and b.key.startswith("contoh_")]
        self.assertEqual(len(examples), 3)
        self.assertIn("tidak aktif", "\n".join(block.value for block in at.sidebar.markdown))
        self.assertEqual(len(at.chat_input), 1)

    def test_kirim_pesan_saat_agent_mati(self):
        at = self.run_app(prompt="Denda tilang naik 150 persen")
        self.assertFalse(at.exception)
        chat = at.session_state["chat"]
        self.assertEqual([turn["role"] for turn in chat], ["user", "assistant"])
        self.assertIn("Belum semua agent aktif", chat[1]["text"])

    def test_laporan_dalam_percakapan(self):
        at = self.run_app(chat=[{"role": "user", "text": REPORT["pesan"]}, {"role": "assistant", "report": REPORT}])
        self.assertFalse(at.exception)
        html = self.html(at)
        self.assertIn("**HOAKS.** Pesan ini memuat klaim yang dibantah sumber", html)
        self.assertIn('class="label">HOAKS', html)
        self.assertIn("1 klaim hoaks, tidak ada klaim yang terbukti benar", html)  # tanpa "aturan 3:"
        self.assertIn("[SALAH] Denda Tilang Naik 150 Persen", html)
        self.assertIn("kutipan sesuai hasil pencarian sumber", html)
        self.assertIn("setelah 1 kali perbaikan", html)
        self.assertIn("Tidak diperiksa karena berupa opini", html)
        self.assertEqual(len([b for b in at.button if b.key and b.key.startswith("contoh_")]), 0)  # sambutan hilang
        self.assertIsNone(re.search(r"\bAI\b|kecerdasan buatan|artificial", html, re.IGNORECASE))

    def test_pola_kata_ai_benar_benar_memeriksa(self):
        # penjaga: memastikan pola di atas memang menangkap kata "AI" (dulu pola ini rusak dan selalu lolos)
        self.assertIsNotNone(re.search(r"\bAI\b", "Diperiksa oleh AI"))
        self.assertIsNone(re.search(r"\bAI\b", "BUKAN KLAIM FAKTUAL"))


if __name__ == "__main__":
    unittest.main()
