"""Menghitung metrik evaluasi dari hasil run_eval (Fase 8; RENCANA §7, Bab 14, 26.8).

    python -m eval.score results/eval/dev_critic                              # satu run
    python -m eval.score results/eval/dev_critic results/eval/dev_tanpa_critic # dibandingkan berdampingan

Per run ditulis skor.json dan skor.md, serta pencocokan.csv: klaim emas <-> klaim sistem. Pencocokan dibuat otomatis
(kemiripan kata, satu-ke-satu). Bila ada yang salah, edit kolom id_sistem (kosongkan = tidak cocok) lalu jalankan
ulang; file yang sudah ada tidak ditimpa. Hapus pencocokan.csv untuk membuat ulang.

Metrik:
- pesan: akurasi, macro-F1 (4 label), matriks kebingungan
- klaim (ujung ke ujung): klaim emas yang tidak ditemukan sistem dihitung salah; juga akurasi klaim yang cocok saja
- dekomposisi: recall (klaim emas yang ditemukan) dan presisi (klaim faktual sistem yang ada padanannya)
- sitasi: % kutipan bukti putusan FAKTA/HOAKS yang memang ada di hasil tool yang diterima Worker (offline,
  sama untuk run dengan/tanpa Critic)
- halusinasi: klaim emas TIDAK TERVERIFIKASI yang diputus FAKTA/HOAKS
- over-abstain: klaim emas FAKTA/HOAKS yang diputus TIDAK TERVERIFIKASI
- Critic: % hasil Worker yang ditolak; untuk klaim yang direvisi, apakah revisi memperbaiki putusan
- per sudah_dicek_media (ya/tidak): akurasi klaim, untuk melihat efek kebocoran
- biaya: token per agent, waktu, pesan A2A, retry LLM
"""
from collections import Counter
import csv
import json
import sys
from pathlib import Path

from agents.critic import quote_in_tool_outputs
from agents.orchestrator import BUKAN_FAKTUAL, SIKAP_KE_PUTUSAN
from mcp_servers.fetch import normalize_text

LABEL_PESAN = ["FAKTA", "HOAKS", "SEBAGIAN BENAR", "TIDAK TERVERIFIKASI"]
LABEL_KLAIM = ["FAKTA", "HOAKS", "TIDAK TERVERIFIKASI"]
TIDAK_DITEMUKAN = "TIDAK DITEMUKAN"  # klaim emas tanpa padanan di klaim sistem
MATCH_THRESHOLD = 0.4
STOPWORDS = set("yang di ke dari dan atau adalah ialah itu ini pada untuk dengan oleh sudah telah akan bahwa sebagai "
                "dalam secara para sang si nya pun juga lah kah ada telah merupakan".split())


# --- pencocokan klaim ------------------------------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    return {word for word in normalize_text(text).split() if word not in STOPWORDS}


def similarity(a: str, b: str) -> float:
    first, second = _tokens(a), _tokens(b)
    return round(len(first & second) / len(first | second), 3) if first | second else 0.0


def match_claims(gold: list[dict], system: list[dict], threshold: float = MATCH_THRESHOLD) -> dict[str, tuple[str, float]]:
    """Satu-ke-satu, serakah dari pasangan paling mirip. {id_klaim emas: (id klaim sistem, skor)}."""
    pairs = sorted(((similarity(g["teks_klaim"], s["teks"]), g["id_klaim"], s["id"]) for g in gold for s in system),
                   reverse=True)
    used_gold, used_system, result = set(), set(), {}
    for score, gold_id, system_id in pairs:
        if score < threshold:
            break
        if gold_id in used_gold or system_id in used_system:
            continue
        used_gold.add(gold_id)
        used_system.add(system_id)
        result[gold_id] = (system_id, score)
    return result


def alignment(results: list[dict], path: Path) -> dict[tuple[str, str], str]:
    """{(id_pesan, id_klaim emas): id klaim sistem atau ""}. Memakai pencocokan.csv bila sudah ada (bisa diedit)."""
    if path.exists():
        with path.open(encoding="utf-8-sig") as file:
            return {(row["id_pesan"], row["id_klaim"]): row["id_sistem"].strip() for row in csv.DictReader(file)}
    rows, mapping = [], {}
    for result in results:
        system = (result.get("laporan") or {}).get("klaim", [])
        by_id = {claim["id"]: claim for claim in system}
        matched = match_claims(result["klaim_emas"], system)
        for gold in result["klaim_emas"]:
            system_id, score = matched.get(gold["id_klaim"], ("", 0.0))
            mapping[(result["id_pesan"], gold["id_klaim"])] = system_id
            rows.append({"id_pesan": result["id_pesan"], "id_klaim": gold["id_klaim"], "teks_emas": gold["teks_klaim"],
                         "id_sistem": system_id, "teks_sistem": by_id.get(system_id, {}).get("teks", ""), "skor": score,
                         "klaim_sistem_lain": " | ".join(f"{c['id']}: {c['teks']}" for c in system if c["id"] != system_id)})
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["id_pesan", "id_klaim", "teks_emas", "id_sistem", "teks_sistem", "skor",
                                                  "klaim_sistem_lain"])
        writer.writeheader()
        writer.writerows(rows)
    return mapping


# --- metrik ----------------------------------------------------------------------------------------------------------

def macro_f1(gold: list[str], predicted: list[str], labels: list[str]) -> float:
    scores = []
    for label in labels:
        tp = sum(g == p == label for g, p in zip(gold, predicted))
        fp = sum(p == label != g for g, p in zip(gold, predicted))
        fn = sum(g == label != p for g, p in zip(gold, predicted))
        if tp + fp + fn == 0:
            continue  # label tidak muncul sama sekali: tidak ikut dirata-rata
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return round(sum(scores) / len(scores), 3) if scores else 0.0


def _rate(numerator: int, denominator: int) -> dict:
    return {"n": numerator, "dari": denominator, "persen": round(100 * numerator / denominator, 1) if denominator else None}


def _accuracy(pairs: list[tuple[str, str]]) -> dict:
    return _rate(sum(g == p for g, p in pairs), len(pairs))


def score_run(run_dir: Path) -> dict:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8")) if (run_dir / "meta.json").exists() else {}
    results = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run_dir.glob("*.json"))
               if p.name not in {"meta.json", "skor.json"}]
    mapping = alignment(results, run_dir / "pencocokan.csv")

    message_pairs, claim_pairs, matched_pairs, by_media = [], [], [], {"ya": [], "tidak": []}
    citations, critic_rounds, critic_rejects = Counter(), 0, 0
    revisions = Counter()
    system_factual, system_factual_matched, gold_non_factual = 0, 0, 0
    cost = Counter()
    for result in results:
        report = result.get("laporan") or {}
        message_pairs.append((result["putusan_emas"], report.get("putusan", "GAGAL")))
        system = {claim["id"]: claim for claim in report.get("klaim", [])}
        matched_ids = set()
        for gold in result["klaim_emas"]:
            system_id = mapping.get((result["id_pesan"], gold["id_klaim"]), "")
            claim = system.get(system_id)
            predicted = claim["putusan"] if claim else TIDAK_DITEMUKAN
            claim_pairs.append((gold["putusan_klaim"], predicted))
            by_media.setdefault(gold.get("sudah_dicek_media", ""), []).append((gold["putusan_klaim"], predicted))
            if claim:
                matched_ids.add(system_id)
                matched_pairs.append((gold["putusan_klaim"], predicted))
                gold_non_factual += predicted == BUKAN_FAKTUAL
                rounds = claim.get("putaran", [])
                if claim.get("revisi") and rounds:
                    first = SIKAP_KE_PUTUSAN.get(rounds[0].get("sikap", ""), "TIDAK TERVERIFIKASI")
                    before, after = first == gold["putusan_klaim"], predicted == gold["putusan_klaim"]
                    revisions["memperbaiki" if after and not before else "memperburuk" if before and not after
                              else "tetap_benar" if after else "tetap_salah"] += 1
        for claim in system.values():
            if claim.get("jenis") != "fakta":
                continue
            system_factual += 1
            system_factual_matched += claim["id"] in matched_ids
            for step in claim.get("putaran", []):
                if step.get("critic"):
                    critic_rounds += 1
                    critic_rejects += step["critic"] == "TOLAK"
            if claim.get("putusan") in {"FAKTA", "HOAKS"}:
                for item in claim.get("bukti", []):
                    valid = quote_in_tool_outputs(item.get("url", ""), item.get("kutipan", ""), claim.get("langkah", []))
                    citations["valid" if valid else "tidak_valid"] += 1
        trace = result.get("trace", {})
        cost["pesan"] += 1
        cost["token_total"] += trace.get("token_total", 0)
        cost["pesan_a2a"] += trace.get("pesan_a2a", 0)
        cost["waktu_detik"] += result.get("waktu_detik", 0)
        for agent, values in trace.get("biaya_per_agent", {}).items():
            cost[f"token_{agent}"] += values.get("token", 0)
            cost["retry_llm"] += values.get("retry", 0)
            cost["tunggu_backoff_detik"] += values.get("tunggu_detik", 0)

    gold_tv = [(g, p) for g, p in matched_pairs if g == "TIDAK TERVERIFIKASI"]
    gold_checkable = [(g, p) for g, p in matched_pairs if g in {"FAKTA", "HOAKS"}]
    n = max(1, cost["pesan"])
    return {
        "run": run_dir.name, "meta": meta, "jumlah_pesan": len(results),
        "pesan": {"akurasi": _accuracy(message_pairs), "macro_f1": macro_f1(*zip(*message_pairs), LABEL_PESAN)
                  if message_pairs else 0.0, "matriks": Counter(f"{g} -> {p}" for g, p in message_pairs)},
        "klaim": {"akurasi_ujung_ke_ujung": _accuracy(claim_pairs),
                  "macro_f1": macro_f1(*zip(*claim_pairs), LABEL_KLAIM) if claim_pairs else 0.0,
                  "akurasi_klaim_cocok": _accuracy(matched_pairs),
                  "matriks": Counter(f"{g} -> {p}" for g, p in claim_pairs),
                  "per_sudah_dicek_media": {key: _accuracy(pairs) for key, pairs in by_media.items() if pairs}},
        "dekomposisi": {"recall": _rate(len(matched_pairs), len(claim_pairs)),
                        "presisi": _rate(system_factual_matched, system_factual),
                        "klaim_emas_dianggap_bukan_faktual": gold_non_factual},
        "sitasi_valid": _rate(citations["valid"], sum(citations.values())),
        "halusinasi": _rate(sum(p in {"FAKTA", "HOAKS"} for _, p in gold_tv), len(gold_tv)),
        "over_abstain": _rate(sum(p == "TIDAK TERVERIFIKASI" for _, p in gold_checkable), len(gold_checkable)),
        "critic": {"ditolak": _rate(critic_rejects, critic_rounds), "revisi": dict(revisions)},
        "biaya_rata_per_pesan": {key: round(value / n, 1) for key, value in cost.items() if key != "pesan"},
    }


# --- laporan ---------------------------------------------------------------------------------------------------------

def _fmt(rate: dict) -> str:
    return "–" if rate["persen"] is None else f"{rate['persen']}% ({rate['n']}/{rate['dari']})"


def format_markdown(scores: list[dict]) -> str:
    head = "| Metrik | " + " | ".join(s["run"] for s in scores) + " |"
    lines = [head, "|---|" + "---|" * len(scores)]

    def row(name, getter):
        lines.append(f"| {name} | " + " | ".join(getter(s) for s in scores) + " |")

    row("Critic", lambda s: "ya" if s["meta"].get("critic", True) else "tidak")
    row("Jumlah pesan", lambda s: str(s["jumlah_pesan"]))
    row("Sidik jari kode", lambda s: s["meta"].get("sidik_jari_kode", "?"))
    row("Akurasi pesan", lambda s: _fmt(s["pesan"]["akurasi"]))
    row("Macro-F1 pesan", lambda s: str(s["pesan"]["macro_f1"]))
    row("Akurasi klaim (ujung ke ujung)", lambda s: _fmt(s["klaim"]["akurasi_ujung_ke_ujung"]))
    row("Macro-F1 klaim", lambda s: str(s["klaim"]["macro_f1"]))
    row("Akurasi klaim yang cocok", lambda s: _fmt(s["klaim"]["akurasi_klaim_cocok"]))
    row("— sudah dicek media", lambda s: _fmt(s["klaim"]["per_sudah_dicek_media"].get("ya", _rate(0, 0))))
    row("— belum dicek media", lambda s: _fmt(s["klaim"]["per_sudah_dicek_media"].get("tidak", _rate(0, 0))))
    row("Dekomposisi: recall", lambda s: _fmt(s["dekomposisi"]["recall"]))
    row("Dekomposisi: presisi", lambda s: _fmt(s["dekomposisi"]["presisi"]))
    row("Klaim emas dianggap bukan faktual", lambda s: str(s["dekomposisi"]["klaim_emas_dianggap_bukan_faktual"]))
    row("Sitasi valid", lambda s: _fmt(s["sitasi_valid"]))
    row("Halusinasi (TV → FAKTA/HOAKS)", lambda s: _fmt(s["halusinasi"]))
    row("Over-abstain (FAKTA/HOAKS → TV)", lambda s: _fmt(s["over_abstain"]))
    row("Critic: hasil Worker ditolak", lambda s: _fmt(s["critic"]["ditolak"]))
    row("Critic: hasil revisi", lambda s: ", ".join(f"{k} {v}" for k, v in sorted(s["critic"]["revisi"].items())) or "–")
    for key in ("token_total", "token_orchestrator", "token_worker", "token_critic", "waktu_detik", "pesan_a2a",
                "retry_llm", "tunggu_backoff_detik"):
        row(f"Rata-rata {key.replace('_', ' ')} / pesan", lambda s, k=key: str(s["biaya_rata_per_pesan"].get(k, 0)))
    for score in scores:
        lines += ["", f"**{score['run']}: matriks pesan (emas -> sistem)**"]
        lines += [f"- {pair}: {count}" for pair, count in sorted(score["pesan"]["matriks"].items())]
        lines += ["", f"**{score['run']}: matriks klaim (emas -> sistem)**"]
        lines += [f"- {pair}: {count}" for pair, count in sorted(score["klaim"]["matriks"].items())]
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv:
        print(__doc__)
        return
    scores = []
    for arg in argv:
        run_dir = Path(arg)
        score = score_run(run_dir)
        (run_dir / "skor.json").write_text(json.dumps(score, ensure_ascii=False, indent=1), encoding="utf-8")
        (run_dir / "skor.md").write_text(format_markdown([score]) + "\n", encoding="utf-8")
        scores.append(score)
    print(format_markdown(scores))
    print("\nPeriksa pencocokan klaim di: " + ", ".join(str(Path(a) / "pencocokan.csv") for a in argv))


if __name__ == "__main__":
    main(sys.argv[1:])
