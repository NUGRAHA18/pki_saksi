"""Tes Fase 8: run_eval (lanjut setelah terhenti, meta) dan score (pencocokan klaim, metrik) dengan data sintetis."""
import csv
import json
import logging
from pathlib import Path
import tempfile
import unittest

import httpx

from agents.common.a2a_client import A2AClient
from agents.common.a2a_server import A2AServer, Completed, agent_card, data_part, make_artifact
from agents.common.audit import AuditLog
from eval.run_eval import check_meta, code_fingerprint, run
from eval.score import TIDAK_DITEMUKAN, macro_f1, match_claims, score_run, similarity

logging.getLogger("asyncio").setLevel(logging.ERROR)

GOLD = [{"id_klaim": "P01-1", "teks_klaim": "Jakarta terletak di Pulau Sumatra", "putusan_klaim": "HOAKS",
         "sudah_dicek_media": "tidak"},
        {"id_klaim": "P01-2", "teks_klaim": "BMKG adalah lembaga pemerintah nonkementerian", "putusan_klaim": "FAKTA",
         "sudah_dicek_media": "tidak"},
        {"id_klaim": "P01-3", "teks_klaim": "Harga beras naik dua kali lipat bulan ini", "putusan_klaim": "TIDAK TERVERIFIKASI",
         "sudah_dicek_media": "ya"}]
TOOL_OUTPUT = json.dumps({"ringkasan": "Jakarta adalah ibu kota di Pulau Jawa", "url": "https://wiki.id/Jakarta"})


def system_claim(cid, teks, putusan, jenis="fakta", putaran=None, revisi=0, kutipan="ibu kota di Pulau Jawa"):
    return {"id": cid, "teks": teks, "jenis": jenis, "putusan": putusan, "revisi": revisi,
            "putaran": putaran or [{"sikap": "MEMBANTAH", "critic": "TERIMA"}],
            "bukti": [{"url": "https://wiki.id/Jakarta", "kutipan": kutipan}],
            "langkah": [{"tool": "ringkasan_wikipedia", "observasi_lengkap": TOOL_OUTPUT}]}


def result(pid, putusan_emas, claims, putusan_sistem, gold=GOLD):
    return {"id_pesan": pid, "kategori": "campuran", "teks_pesan": "x", "putusan_emas": putusan_emas,
            "klaim_emas": gold, "status_task": "completed",
            "laporan": {"putusan": putusan_sistem, "klaim": claims} if putusan_sistem else None,
            "trace": {"token_total": 1000, "pesan_a2a": 6,
                      "biaya_per_agent": {"worker": {"token": 900, "retry": 2, "tunggu_detik": 15.0},
                                          "critic": {"token": 100, "retry": 0, "tunggu_detik": 0.0}}},
            "waktu_detik": 100.0}


class TestPencocokan(unittest.TestCase):
    def test_kemiripan(self):
        self.assertEqual(similarity("Jakarta terletak di Pulau Sumatra", "Jakarta adalah terletak di Pulau Sumatra."), 1.0)
        self.assertLess(similarity("Jakarta terletak di Pulau Sumatra", "Jakarta adalah ibu kota Indonesia"), 0.4)

    def test_satu_ke_satu(self):
        system = [{"id": "K1", "teks": "Jakarta adalah ibu kota Indonesia"},
                  {"id": "K2", "teks": "Jakarta terletak di Pulau Sumatra"}]
        matched = match_claims(GOLD, system)
        self.assertEqual(matched["P01-1"][0], "K2")
        self.assertNotIn("P01-2", matched)  # tidak ada klaim BMKG di sistem

    def test_macro_f1(self):
        self.assertEqual(macro_f1(["A", "B"], ["A", "B"], ["A", "B", "C"]), 1.0)
        self.assertEqual(macro_f1(["A", "A"], ["B", "B"], ["A", "B"]), 0.0)


class TestScore(unittest.TestCase):
    def write_run(self, folder: Path, results: list[dict], critic=True) -> Path:
        run_dir = folder / "dev_uji"
        run_dir.mkdir()
        (run_dir / "meta.json").write_text(json.dumps({"critic": critic, "sidik_jari_kode": "abc"}), encoding="utf-8")
        for item in results:
            (run_dir / f"{item['id_pesan']}.json").write_text(json.dumps(item), encoding="utf-8")
        return run_dir

    def test_metrik(self):
        claims = [
            system_claim("K1", "Jakarta terletak di Pulau Sumatra", "HOAKS", revisi=1,
                         putaran=[{"sikap": "MENDUKUNG", "critic": "TOLAK"}, {"sikap": "MEMBANTAH", "critic": "TERIMA"}]),
            system_claim("K2", "Harga beras naik dua kali lipat bulan ini", "FAKTA", kutipan="kutipan karangan"),
            system_claim("K3", "Pemerintah sangat jahat sekali", "BUKAN KLAIM FAKTUAL", jenis="opini"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            run_dir = self.write_run(Path(folder), [result("P01", "SEBAGIAN BENAR", claims, "HOAKS"),
                                                    result("P02", "HOAKS", [], None, gold=[])])
            score = score_run(run_dir)
            alignment_rows = list(csv.DictReader((run_dir / "pencocokan.csv").read_text(encoding="utf-8").splitlines()))
        self.assertEqual(score["pesan"]["akurasi"]["n"], 0)
        self.assertIn("HOAKS -> GAGAL", score["pesan"]["matriks"])  # pesan tanpa laporan dihitung salah
        self.assertEqual(score["klaim"]["matriks"][f"FAKTA -> {TIDAK_DITEMUKAN}"], 1)
        self.assertEqual(score["klaim"]["akurasi_ujung_ke_ujung"]["n"], 1)  # hanya P01-1 yang benar
        self.assertEqual((score["dekomposisi"]["recall"]["n"], score["dekomposisi"]["recall"]["dari"]), (2, 3))
        self.assertEqual((score["dekomposisi"]["presisi"]["n"], score["dekomposisi"]["presisi"]["dari"]), (2, 2))
        self.assertEqual(score["sitasi_valid"]["n"], 1)  # kutipan K1 ada di hasil tool, K2 karangan
        self.assertEqual(score["sitasi_valid"]["dari"], 2)
        self.assertEqual(score["halusinasi"]["n"], 1)  # emas TIDAK TERVERIFIKASI diputus FAKTA
        self.assertEqual(score["critic"]["ditolak"]["n"], 1)
        self.assertEqual(score["critic"]["revisi"], {"memperbaiki": 1})
        self.assertEqual(score["klaim"]["per_sudah_dicek_media"]["ya"]["n"], 0)
        self.assertEqual(score["biaya_rata_per_pesan"]["token_worker"], 900.0)
        self.assertEqual(len(alignment_rows), 3)

    def test_pencocokan_manual_dipakai(self):
        claims = [system_claim("K1", "Ibu kota Indonesia berada di Sumatra", "HOAKS")]
        with tempfile.TemporaryDirectory() as folder:
            run_dir = self.write_run(Path(folder), [result("P01", "HOAKS", claims, "HOAKS", gold=GOLD[:1])])
            self.assertEqual(score_run(run_dir)["dekomposisi"]["recall"]["n"], 0)  # terlalu berbeda untuk otomatis
            path = run_dir / "pencocokan.csv"
            rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
            rows[0]["id_sistem"] = "K1"  # dikoreksi manual
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            self.assertEqual(score_run(run_dir)["klaim"]["akurasi_ujung_ke_ujung"]["n"], 1)


class FakeOrchestrator:
    def __init__(self) -> None:
        self.received = []
        card = agent_card("orchestrator", "palsu", "http://orch/", [{"id": "periksa_pesan", "name": "x"}])
        self.server = A2AServer(card, self.handle)

    async def handle(self, ctx):
        self.received.append(ctx.text())
        report = {"putusan": "HOAKS", "klaim": []}
        return Completed([make_artifact("laporan", [data_part(report)])])


class TestRunEval(unittest.IsolatedAsyncioTestCase):
    async def test_lanjut_setelah_terhenti(self):
        orchestrator = FakeOrchestrator()
        messages = [{"id_pesan": f"P0{i}", "kategori": "hoaks", "teks_pesan": f"pesan {i}", "putusan_pesan": "HOAKS",
                     "klaim_emas": []} for i in (1, 2, 3)]

        def factory():
            return A2AClient("http://orch", token="", transport=httpx.ASGITransport(app=orchestrator.server.app))

        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            (out / "P01.json").write_text("{}", encoding="utf-8")  # sudah selesai di run sebelumnya
            audit = AuditLog(out / "audit.jsonl")
            done = await run(messages, out, factory, audit)
            saved = json.loads((out / "P02.json").read_text(encoding="utf-8"))
        self.assertEqual(done, ["P02", "P03"])
        self.assertEqual(orchestrator.received, ["pesan 2", "pesan 3"])
        self.assertEqual((saved["laporan"]["putusan"], saved["putusan_emas"], saved["status_task"]),
                         ("HOAKS", "HOAKS", "completed"))

    def test_meta_berbeda_ditolak(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "meta.json"
            meta = {"nama": "dev", "bagian": "dev", "critic": True, "model": "m", "sidik_jari_kode": "a",
                    "konfigurasi": {}}
            check_meta(path, meta)
            check_meta(path, meta)  # sama: boleh dilanjutkan
            with self.assertRaisesRegex(SystemExit, "sidik_jari_kode"):
                check_meta(path, {**meta, "sidik_jari_kode": "b"})

    def test_sidik_jari_kode_stabil(self):
        self.assertEqual(code_fingerprint(), code_fingerprint())
        self.assertEqual(len(code_fingerprint()), 16)


if __name__ == "__main__":
    unittest.main()
