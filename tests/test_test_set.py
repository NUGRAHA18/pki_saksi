"""Tes Fase 7: validator, pembagian dev/held-out, dan blocklist test set (data sintetis khusus tes)."""
from collections import Counter
from pathlib import Path
import tempfile
import unittest

from eval.test_set import (TARGET, blind_rows, blocklist_entries, cohen_kappa, compare, complete, load, read_csv,
                            split, validate)
from mcp_servers.common import is_blocked


def pesan(pid, kategori="hoaks", putusan="HOAKS", **kwargs) -> dict:
    row = {"id_pesan": pid, "kategori": kategori, "teks_pesan": f"pesan uji nomor {pid} sebarkan ke semua",
           "putusan_pesan": putusan, "ditulis_ulang": "ya", "sumber_asli": "", "pengumpul": "Pengumpul",
           "pemeriksa_silang": "Claude (AI)", "catatan": ""}
    return {**row, **kwargs}


def klaim(pid, n, putusan="HOAKS", **kwargs) -> dict:
    row = {"id_pesan": pid, "id_klaim": f"{pid}-{n}", "teks_klaim": f"klaim uji nomor {pid} {n}",
           "putusan_klaim": putusan, "sumber_label": "https://turnbackhoax.id/articles/1" if putusan != "TIDAK TERVERIFIKASI" else "",
           "sudah_dicek_media": "ya"}
    return {**row, **kwargs}


class TestValidate(unittest.TestCase):
    def test_data_benar_tanpa_error(self):
        report = validate([pesan("P01"), pesan("P02", "campuran", "SEBAGIAN BENAR", ditulis_ulang="tidak")],
                          [klaim("P01", 1), klaim("P02", 1), klaim("P02", 2, "FAKTA")])
        self.assertEqual(report.errors, [])

    def test_putusan_pesan_harus_sesuai_aturan_agregasi(self):
        report = validate([pesan("P01", "campuran", "HOAKS")], [klaim("P01", 1), klaim("P01", 2, "FAKTA")])
        self.assertTrue(any("seharusnya SEBAGIAN BENAR" in e for e in report.errors))

    def test_error_umum(self):
        report = validate(
            [pesan("P01", "aneh"), pesan("P01"), pesan("P02", pemeriksa_silang="pengumpul"), pesan("P03")],
            [klaim("P01", 1, sumber_label=""), klaim("P02", 1, "BUKAN KLAIM FAKTUAL"), klaim("P09", 1),
             klaim("P02", 2, sumber_label="turnbackhoax.id/x")])
        text = "\n".join(report.errors)
        for expected in ["kategori 'aneh'", "P01 muncul 2 kali", "harus orang lain", "P03: tidak punya klaim",
                         "wajib punya sumber_label", "opini/prediksi/ajakan tidak ditulis", "P09 tidak ada",
                         "bukan URL"]:
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_peringatan_jumlah_dan_pemeriksaan_silang(self):
        report = validate([pesan("P01", pemeriksa_silang="")], [klaim("P01", 1)])
        self.assertEqual(report.errors, [])
        self.assertTrue(any("belum diperiksa silang" in w for w in report.warnings))
        self.assertTrue(any("kategori fakta: 0 pesan (target 10)" in w for w in report.warnings))

    def test_kolom_hilang(self):
        report = validate([{"id_pesan": "P01"}], [])
        self.assertIn("kolom hilang", report.errors[0])


class TestSplit(unittest.TestCase):
    def rows(self):
        rows, n = [], 0
        for kategori, target in TARGET.items():
            for _ in range(target):
                n += 1
                rows.append(pesan(f"P{n:02d}", kategori))
        return rows

    def test_stratifikasi_10_dev_30_heldout(self):
        parts = split(self.rows())
        self.assertEqual(Counter(parts.values()), {"dev": 10, "heldout": 30})
        by_category = Counter((row["kategori"], parts[row["id_pesan"]]) for row in self.rows())
        self.assertEqual(by_category[("hoaks", "dev")], 4)  # round(15 * 0.25)
        self.assertEqual(by_category[("tidak_terverifikasi", "dev")], 1)

    def test_deterministik(self):
        self.assertEqual(split(self.rows()), split(list(reversed(self.rows()))))


class TestLengkapiOtomatis(unittest.TestCase):
    def test_id_klaim_dan_putusan_pesan_dilengkapi(self):
        pesan_rows = [pesan("P01", "campuran", ""), pesan("P02", "hoaks", "HOAKS")]
        klaim_rows = [klaim("P01", 1, "HOAKS", id_klaim=""), klaim("P01", 2, "FAKTA", id_klaim=""), klaim("P02", 1)]
        filled_pesan, filled_klaim = complete(pesan_rows, klaim_rows)
        self.assertEqual([row["id_klaim"] for row in filled_klaim], ["P01-1", "P01-2", "P02-1"])
        self.assertEqual([row["putusan_pesan"] for row in filled_pesan], ["SEBAGIAN BENAR", "HOAKS"])
        self.assertEqual(validate(filled_pesan, filled_klaim).errors, [])

    def test_isian_manual_yang_salah_tetap_tertangkap(self):
        filled_pesan, filled_klaim = complete([pesan("P01", "campuran", "FAKTA")],
                                              [klaim("P01", 1, "HOAKS"), klaim("P01", 2, "FAKTA")])
        self.assertTrue(any("seharusnya SEBAGIAN BENAR" in e for e in validate(filled_pesan, filled_klaim).errors))

    def test_file_contoh_valid(self):
        self.assertEqual(validate(*load(prefix="contoh_")).errors, [])


class TestPemeriksaanSilang(unittest.TestCase):
    def test_ekspor_buta_tanpa_label(self):
        rows = blind_rows([pesan("P01")], [klaim("P01", 1)])
        self.assertEqual(set(rows[0]), {"id_klaim", "id_pesan", "teks_pesan", "teks_klaim", "sumber_label"})
        self.assertNotIn("HOAKS", " ".join(rows[0].values()))
        self.assertIn("pesan uji nomor P01", rows[0]["teks_pesan"])

    def test_bandingkan(self):
        mine = [klaim("P01", 1, "HOAKS"), klaim("P01", 2, "FAKTA"), klaim("P02", 1, "HOAKS"), klaim("P03", 1, "FAKTA")]
        checker = [{"id_klaim": "P01-1", "putusan_klaim": "HOAKS", "alasan": ""},
                   {"id_klaim": "P01-2", "putusan_klaim": "TIDAK TERVERIFIKASI", "alasan": "sumber tidak menyebut angka"},
                   {"id_klaim": "P02-1", "putusan_klaim": "HOAKS", "alasan": ""},
                   {"id_klaim": "P09-1", "putusan_klaim": "FAKTA", "alasan": ""}]
        result = compare(mine, checker)
        self.assertEqual((result["jumlah"], result["setuju"], result["persen_setuju"]), (3, 2, 66.7))
        self.assertEqual(result["berbeda"][0]["alasan_pemeriksa"], "sumber tidak menyebut angka")
        self.assertEqual((result["belum_dilabeli_pemeriksa"], result["tidak_ada_di_klaim_csv"]), (["P03-1"], ["P09-1"]))

    def test_kappa(self):
        self.assertEqual(cohen_kappa(["A", "B", "A", "B"], ["A", "B", "A", "B"]), 1.0)
        self.assertEqual(cohen_kappa(["A", "A", "B", "B"], ["A", "B", "A", "B"]), 0.0)  # setara kebetulan
        self.assertEqual(cohen_kappa(["A", "A"], ["A", "A"]), 1.0)


class TestBlocklistDanCsv(unittest.TestCase):
    def test_blocklist_hanya_sumber_yang_sudah_dicek_media(self):
        entries = blocklist_entries(
            [klaim("P01", 1, sumber_label="https://www.kompas.com/cekfakta/read/1; https://turnbackhoax.id/articles/5#x"),
             klaim("P02", 1, "FAKTA", sumber_label="https://bmkg.go.id/berita/2", sudah_dicek_media="tidak")],
            [pesan("P01", sumber_asli="https://turnbackhoax.id/articles/7"),
             pesan("P02", sumber_asli="https://www.facebook.com/post/9")])
        self.assertEqual(entries, ["kompas.com/cekfakta/read/1", "turnbackhoax.id/articles/5", "turnbackhoax.id/articles/7"])
        self.assertTrue(is_blocked("https://www.kompas.com/cekfakta/read/1", entries))
        self.assertFalse(is_blocked("https://www.kompas.com/berita/lain", entries))
        self.assertFalse(is_blocked("https://bmkg.go.id/berita/2", entries))  # sumber resmi klaim FAKTA tetap boleh
        self.assertFalse(is_blocked("https://turnbackhoax.id/articles/99", entries))  # tidak ada blokir per domain

    def test_csv_titik_koma_dan_bom(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pesan.csv"
            path.write_text("﻿id_pesan;kategori;teks_pesan\nP01;hoaks;Ayo, sebarkan!\n", encoding="utf-8")
            self.assertEqual(read_csv(path), [{"id_pesan": "P01", "kategori": "hoaks", "teks_pesan": "Ayo, sebarkan!"}])
            path.write_text("id_pesan,kategori\n", encoding="utf-8")
            self.assertEqual(read_csv(path), [])


if __name__ == "__main__":
    unittest.main()
