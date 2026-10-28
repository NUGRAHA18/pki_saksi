"""Perangkat test set (Fase 7): validasi, pembagian dev/held-out, dan blocklist sumber label.

File (lihat data/test_set/PANDUAN.md):
    data/test_set/pesan.csv   satu baris per pesan
    data/test_set/klaim.csv   satu baris per klaim faktual
    data/test_set/split.csv   hasil `bagi`: id_pesan -> dev/heldout (dibekukan)

    python -m eval.test_set cek          # periksa isi; kode keluar 1 bila ada error (--contoh: file contoh)
    python -m eval.test_set ekspor-buta  # label_buta.csv: klaim tanpa label, untuk pemeriksa silang
    python -m eval.test_set bandingkan   # bandingkan klaim.csv dengan klaim_pemeriksa.csv
    python -m eval.test_set bagi         # buat split.csv (menolak menimpa split yang sudah ada)
    python -m eval.test_set blocklist    # tulis data/test_set/blocklist_eval.txt (dipakai run_eval saja)

Pemeriksaan silang (sejak 28 Okt test set dikerjakan satu orang): pemeriksa kedua (AI) melabeli label_buta.csv tanpa
melihat label pengumpul, lalu `bandingkan` menghitung kesepakatan dan menampilkan perbedaan untuk diputuskan pengumpul.
"""
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass, field
import random
import re
import sys
from pathlib import Path

from agents.common.config import ROOT
from agents.orchestrator import BUKAN_FAKTUAL, aggregate

DIR = ROOT / "data" / "test_set"
PESAN_COLUMNS = ["id_pesan", "kategori", "teks_pesan", "putusan_pesan", "ditulis_ulang", "sumber_asli", "pengumpul",
                 "pemeriksa_silang", "catatan"]
KLAIM_COLUMNS = ["id_pesan", "id_klaim", "teks_klaim", "putusan_klaim", "sumber_label", "sudah_dicek_media"]
TARGET = {"hoaks": 15, "fakta": 10, "campuran": 10, "tidak_terverifikasi": 5}  # RENCANA §7
PUTUSAN_PESAN = {"FAKTA", "HOAKS", "SEBAGIAN BENAR", "TIDAK TERVERIFIKASI"}
PUTUSAN_KLAIM = {"FAKTA", "HOAKS", "TIDAK TERVERIFIKASI"}
DEV_SHARE = 0.25  # 10 dari 40
SEED = 2026


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [f"ERROR ({len(self.errors)}):"] + [f"  - {e}" for e in self.errors]
        lines += [f"PERINGATAN ({len(self.warnings)}):"] + [f"  - {w}" for w in self.warnings]
        return "\n".join(lines)


def read_csv(path: Path) -> list[dict]:
    """CSV dari Google Sheets (koma) atau Excel Indonesia (titik koma), UTF-8 dengan/tanpa BOM."""
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return []
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;")
    return [{key.strip(): (value or "").strip() for key, value in row.items() if key}
            for row in csv.DictReader(text.splitlines(), dialect=dialect)]


def complete(pesan: list[dict], klaim: list[dict]) -> tuple[list[dict], list[dict]]:
    """Melengkapi kolom yang boleh dikosongkan pengisi: id_klaim (urutan klaim per pesan: P07-1, P07-2, ...) dan
    putusan_pesan (dihitung dari label klaim dengan aturan agregasi sistem). Kolom yang sudah diisi tidak diubah,
    supaya isian manual yang keliru tetap tertangkap validator."""
    counter: Counter = Counter()
    filled_claims = []
    for row in klaim:
        counter[row.get("id_pesan", "")] += 1
        filled_claims.append({**row, "id_klaim": row.get("id_klaim") or f"{row.get('id_pesan', '')}-{counter[row.get('id_pesan', '')]}"})
    labels: dict[str, list[str]] = defaultdict(list)
    for row in filled_claims:
        labels[row.get("id_pesan", "")].append(row.get("putusan_klaim", ""))
    filled_messages = [{**row, "putusan_pesan": row.get("putusan_pesan") or (aggregate(labels[row["id_pesan"]])[0]
                                                                             if labels.get(row.get("id_pesan")) else "")}
                       for row in pesan]
    return filled_messages, filled_claims


def load(folder: Path = DIR, prefix: str = "") -> tuple[list[dict], list[dict]]:
    """Membaca pesan.csv dan klaim.csv (atau contoh_pesan.csv dan contoh_klaim.csv dengan prefix="contoh_"),
    lalu melengkapi kolom yang dikosongkan."""
    return complete(read_csv(folder / f"{prefix}pesan.csv"), read_csv(folder / f"{prefix}klaim.csv"))


def _urls(value: str) -> list[str]:
    return [url.strip() for url in value.split(";") if url.strip()]


def validate(pesan: list[dict], klaim: list[dict]) -> Report:
    report = Report()
    for name, rows, columns in (("pesan.csv", pesan, PESAN_COLUMNS), ("klaim.csv", klaim, KLAIM_COLUMNS)):
        if rows and (missing := [c for c in columns if c not in rows[0]]):
            report.errors.append(f"{name}: kolom hilang {missing}")
    if report.errors:
        return report

    ids = Counter(row["id_pesan"] for row in pesan)
    report.errors += [f"pesan.csv: id_pesan {i} muncul {n} kali" for i, n in ids.items() if n > 1]
    texts = Counter(" ".join(row["teks_pesan"].lower().split()) for row in pesan)
    report.errors += [f"pesan.csv: teks pesan sama persis muncul {n} kali: \"{t[:60]}\"" for t, n in texts.items() if n > 1]
    claims_of: dict[str, list[dict]] = defaultdict(list)
    for row in klaim:
        claims_of[row["id_pesan"]].append(row)

    for row in pesan:
        pid = row["id_pesan"] or "(tanpa id)"
        if row["kategori"] not in TARGET:
            report.errors.append(f"{pid}: kategori '{row['kategori']}' tidak dikenal ({sorted(TARGET)})")
        if row["putusan_pesan"] not in PUTUSAN_PESAN:
            report.errors.append(f"{pid}: putusan_pesan '{row['putusan_pesan']}' tidak dikenal ({sorted(PUTUSAN_PESAN)})")
        if len(row["teks_pesan"].split()) < 5:
            report.errors.append(f"{pid}: teks_pesan terlalu pendek")
        if row["ditulis_ulang"] not in {"ya", "tidak"}:
            report.errors.append(f"{pid}: ditulis_ulang harus 'ya' atau 'tidak'")
        if not row["pemeriksa_silang"]:
            report.warnings.append(f"{pid}: belum diperiksa silang")
        elif row["pemeriksa_silang"].lower() == row["pengumpul"].lower():
            report.errors.append(f"{pid}: pemeriksa_silang harus orang lain, bukan pengumpul")
        claims = claims_of.get(row["id_pesan"], [])
        if not claims:
            report.errors.append(f"{pid}: tidak punya klaim di klaim.csv")
            continue
        labels = [c["putusan_klaim"] for c in claims]
        expected, rule = aggregate(labels)
        if row["putusan_pesan"] in PUTUSAN_PESAN and expected != row["putusan_pesan"]:
            report.errors.append(f"{pid}: putusan_pesan {row['putusan_pesan']}, padahal dari label klaim seharusnya "
                                 f"{expected} ({rule})")
        if row["kategori"] == "campuran" and not ({"FAKTA", "HOAKS"} <= set(labels)):
            report.warnings.append(f"{pid}: kategori campuran sebaiknya punya klaim FAKTA dan HOAKS")

    known = set(ids)
    seen_claims = Counter(row["id_klaim"] for row in klaim)
    report.errors += [f"klaim.csv: id_klaim {i} muncul {n} kali" for i, n in seen_claims.items() if n > 1]
    for row in klaim:
        cid = row["id_klaim"] or f"(klaim tanpa id di {row['id_pesan']})"
        if row["id_pesan"] not in known:
            report.errors.append(f"{cid}: id_pesan {row['id_pesan']} tidak ada di pesan.csv")
        if row["putusan_klaim"] not in PUTUSAN_KLAIM:
            hint = " (opini/prediksi/ajakan tidak ditulis di klaim.csv)" if row["putusan_klaim"] == BUKAN_FAKTUAL else ""
            report.errors.append(f"{cid}: putusan_klaim '{row['putusan_klaim']}' tidak dikenal{hint}")
        urls = _urls(row["sumber_label"])
        if row["putusan_klaim"] in {"FAKTA", "HOAKS"} and not urls:
            report.errors.append(f"{cid}: label {row['putusan_klaim']} wajib punya sumber_label")
        report.errors += [f"{cid}: sumber_label bukan URL: {u}" for u in urls if not re.match(r"https?://\S+$", u)]
        if row["sudah_dicek_media"] not in {"ya", "tidak"}:
            report.errors.append(f"{cid}: sudah_dicek_media harus 'ya' atau 'tidak'")
        if len(row["teks_klaim"].split()) < 3:
            report.errors.append(f"{cid}: teks_klaim terlalu pendek")

    counts = Counter(row["kategori"] for row in pesan)
    for kategori, target in TARGET.items():
        if counts[kategori] != target:
            report.warnings.append(f"kategori {kategori}: {counts[kategori]} pesan (target {target})")
    rewritten = sum(row["ditulis_ulang"] == "ya" for row in pesan)
    if pesan and not 0.3 <= rewritten / len(pesan) <= 0.7:
        report.warnings.append(f"{rewritten}/{len(pesan)} pesan ditulis ulang (usul ±separuh)")
    return report


def split(pesan: list[dict], dev_share: float = DEV_SHARE, seed: int = SEED) -> dict[str, str]:
    """Stratifikasi per kategori. Total dev = dev_share x jumlah pesan, dibagi ke kategori secara proporsional dengan
    metode sisa terbesar (round() Python membulatkan 2,5 ke 2, sehingga totalnya bisa meleset)."""
    rng = random.Random(seed)
    by_category: dict[str, list[str]] = defaultdict(list)
    for row in sorted(pesan, key=lambda r: r["id_pesan"]):
        by_category[row["kategori"]].append(row["id_pesan"])
    exact = {kategori: len(ids) * dev_share for kategori, ids in by_category.items()}
    quota = {kategori: int(value) for kategori, value in exact.items()}
    remaining = int(len(pesan) * dev_share + 0.5) - sum(quota.values())
    for kategori in sorted(exact, key=lambda k: (-(exact[k] - quota[k]), k))[:max(0, remaining)]:
        quota[kategori] += 1
    result = {}
    for kategori in sorted(by_category):
        shuffled = by_category[kategori][:]
        rng.shuffle(shuffled)
        result.update({pid: "dev" if i < quota[kategori] else "heldout" for i, pid in enumerate(shuffled)})
    return result


BLIND_COLUMNS = ["id_klaim", "id_pesan", "teks_pesan", "teks_klaim", "sumber_label"]


def blind_rows(pesan: list[dict], klaim: list[dict]) -> list[dict]:
    """Klaim tanpa label pengumpul (putusan_klaim, sudah_dicek_media, catatan tidak ikut). Sumber label tetap
    disertakan: pemeriksa menilai apakah sumber itu memang membuktikan klaim, bukan mencari ulang dari nol."""
    text_of = {row["id_pesan"]: row["teks_pesan"] for row in pesan}
    return [{"id_klaim": row["id_klaim"], "id_pesan": row["id_pesan"], "teks_pesan": text_of.get(row["id_pesan"], ""),
             "teks_klaim": row["teks_klaim"], "sumber_label": row["sumber_label"]} for row in klaim]


def cohen_kappa(first: list[str], second: list[str]) -> float:
    n = len(first)
    if not n:
        return 0.0
    observed = sum(a == b for a, b in zip(first, second)) / n
    count_a, count_b = Counter(first), Counter(second)
    expected = sum(count_a[label] * count_b[label] for label in set(first) | set(second)) / (n * n)
    return 1.0 if expected == 1 else round((observed - expected) / (1 - expected), 3)


def compare(klaim: list[dict], checker: list[dict]) -> dict:
    """Bandingkan label pengumpul (klaim.csv) dengan label pemeriksa (klaim_pemeriksa.csv) per id_klaim."""
    mine = {row["id_klaim"]: row for row in klaim}
    theirs = {row["id_klaim"]: row for row in checker}
    shared = [cid for cid in mine if cid in theirs]
    pairs = [(mine[cid]["putusan_klaim"], theirs[cid]["putusan_klaim"]) for cid in shared]
    differences = [{"id_klaim": cid, "teks_klaim": mine[cid]["teks_klaim"], "pengumpul": a, "pemeriksa": b,
                    "alasan_pemeriksa": theirs[cid].get("alasan", "")}
                   for cid, (a, b) in zip(shared, pairs) if a != b]
    return {
        "jumlah": len(shared),
        "setuju": len(shared) - len(differences),
        "persen_setuju": round(100 * (len(shared) - len(differences)) / len(shared), 1) if shared else 0.0,
        "kappa": cohen_kappa([a for a, _ in pairs], [b for _, b in pairs]),
        "matriks": Counter(f"{a} / {b}" for a, b in pairs),
        "berbeda": differences,
        "belum_dilabeli_pemeriksa": [cid for cid in mine if cid not in theirs],
        "tidak_ada_di_klaim_csv": [cid for cid in theirs if cid not in mine],
    }


def format_comparison(result: dict) -> str:
    lines = [f"{result['jumlah']} klaim dibandingkan | setuju {result['setuju']} ({result['persen_setuju']}%) | "
             f"Cohen's kappa {result['kappa']}", "", "matriks (pengumpul / pemeriksa):"]
    lines += [f"  {pair:<45} {n}" for pair, n in sorted(result["matriks"].items())]
    lines += ["", f"BERBEDA ({len(result['berbeda'])}) — putuskan label akhirnya di klaim.csv:"]
    for item in result["berbeda"]:
        lines.append(f"  {item['id_klaim']}: kamu {item['pengumpul']}, pemeriksa {item['pemeriksa']} — {item['teks_klaim']}")
        if item["alasan_pemeriksa"]:
            lines.append(f"      alasan pemeriksa: {item['alasan_pemeriksa']}")
    for key, title in (("belum_dilabeli_pemeriksa", "belum dilabeli pemeriksa"),
                       ("tidak_ada_di_klaim_csv", "ada di klaim_pemeriksa.csv tetapi tidak di klaim.csv")):
        if result[key]:
            lines.append(f"\n{title}: {', '.join(result[key])}")
    return "\n".join(lines)


def blocklist_entries(klaim: list[dict], pesan: list[dict]) -> list[str]:
    """Opsi (b), keputusan 28 Okt: hanya sumber label klaim yang sudah_dicek_media = ya (artikel cek fakta), ditambah
    sumber_asli pesan yang memuat klaim seperti itu. Sumber resmi untuk klaim FAKTA yang belum pernah dicek media
    TIDAK diblokir, supaya bukti sah itu tetap bisa dipakai sistem. Tidak ada blokir per domain."""
    checked = [row for row in klaim if row["sudah_dicek_media"] == "ya"]
    messages = {row["id_pesan"] for row in checked}
    urls = {u for row in checked for u in _urls(row["sumber_label"])}
    urls |= {u for row in pesan if row["id_pesan"] in messages for u in _urls(row["sumber_asli"])}
    return sorted({re.sub(r"^https?://(www\.)?", "", url).split("#")[0].rstrip("/") for url in urls})


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    command = argv[0] if argv else "cek"
    # `cek --contoh` memeriksa file contoh (contoh_pesan.csv, contoh_klaim.csv)
    pesan, klaim = load(DIR, "contoh_" if "--contoh" in argv else "")
    if command == "cek":
        report = validate(pesan, klaim)
        print(f"{len(pesan)} pesan, {len(klaim)} klaim | per kategori: {dict(Counter(r['kategori'] for r in pesan))}")
        print(report.text())
        return 1 if report.errors else 0
    if command == "ekspor-buta":
        report = validate(pesan, klaim)
        if report.errors:
            print("Test set masih punya error; jalankan `cek` dulu.\n" + report.text())
            return 1
        path = DIR / "label_buta.csv"
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=BLIND_COLUMNS)
            writer.writeheader()
            writer.writerows(blind_rows(pesan, klaim))
        print(f"{len(klaim)} klaim tanpa label ditulis ke {path}\n"
              f"Pemeriksa menulis hasilnya ke {DIR / 'klaim_pemeriksa.csv'} (kolom: id_klaim, putusan_klaim, alasan)")
        return 0
    if command == "bandingkan":
        path = DIR / "klaim_pemeriksa.csv"
        if not path.exists():
            print(f"{path} belum ada. Jalankan `ekspor-buta`, lalu minta pemeriksa melabeli label_buta.csv.")
            return 1
        checker = read_csv(path)
        invalid = [row["id_klaim"] for row in checker if row.get("putusan_klaim") not in PUTUSAN_KLAIM]
        if invalid:
            print(f"klaim_pemeriksa.csv berisi label tidak dikenal pada: {invalid}")
            return 1
        print(format_comparison(compare(klaim, checker)))
        return 0
    if command == "bagi":
        path = DIR / "split.csv"
        if path.exists() and "--paksa" not in argv:
            print(f"{path} sudah ada (dibekukan). Pakai --paksa hanya bila memang disepakati untuk dibuat ulang.")
            return 1
        report = validate(pesan, klaim)
        if report.errors:
            print("Test set masih punya error; jalankan `cek` dulu.\n" + report.text())
            return 1
        parts = split(pesan)
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["id_pesan", "bagian"])
            writer.writerows(sorted(parts.items()))
        print(f"split.csv ditulis: {dict(Counter(parts.values()))} (seed {SEED})")
        return 0
    if command == "blocklist":
        entries = blocklist_entries(klaim, pesan)
        # bukan data/blocklist.txt (permanen): blocklist evaluasi hanya aktif selama run_eval, lewat SAKSI_BLOCKLIST_FILE
        path = DIR / "blocklist_eval.txt"
        path.write_text("# dibuat oleh: python -m eval.test_set blocklist\n" + "\n".join(entries) + "\n", encoding="utf-8")
        print(f"{len(entries)} entri ditulis ke {path}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
