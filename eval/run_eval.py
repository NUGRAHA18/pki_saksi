"""Menjalankan test set lewat sistem SAKSI (Fase 8; Bab 14, 26.8).

Pesan dikirim ke Orchestrator satu per satu (paralel antar-pesan akan menghabiskan kuota Gemma per menit). Setiap
pesan disimpan ke filenya sendiri, jadi run yang terhenti (kuota habis, komputer mati) bisa dilanjutkan dengan
perintah yang sama: pesan yang sudah selesai dilewati.

    python -m eval.run_eval --bagian dev                       # dengan Critic
    python -m eval.run_eval --bagian dev --tanpa-critic        # pembanding: tanpa Critic
    python -m eval.run_eval --bagian heldout --nama heldout_v1 # held-out: hanya setelah sistem dibekukan

Hasil: results/eval/<nama>/
    meta.json           konfigurasi + sidik jari kode (bukti sistem tidak berubah selama run)
    <id_pesan>.json     laporan Orchestrator + label emas + ringkasan trace (token/waktu per agent)
Hitung metrik: python -m eval.score results/eval/<nama>

Blocklist sumber label (data/test_set/blocklist_eval.txt) aktif hanya selama run ini, lewat SAKSI_BLOCKLIST_FILE.
"""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from agents.common.audit import AuditLog, new_correlation_id
from agents.common.config import ROOT
from eval.test_set import DIR as TEST_SET_DIR, load, read_csv, validate

RESULTS = ROOT / "results" / "eval"
ORCHESTRATOR_URL = "http://127.0.0.1:8100"
MESSAGE_TIMEOUT = 4000  # detik; sama dengan batas run_all --pesan
CODE_FILES = ["agents/orchestrator.py", "agents/worker.py", "agents/critic.py", "agents/worker_server.py",
              "agents/common/llm.py", "mcp_servers/cekfakta.py", "mcp_servers/wiki.py", "mcp_servers/berita.py",
              "mcp_servers/fetch.py", "mcp_servers/common.py", "mcp_servers/web.py"]


def code_fingerprint(root: Path = ROOT) -> str:
    """Hash isi file yang menentukan perilaku sistem (prompt, guardrail, tool). Berubah = sistem berubah."""
    digest = hashlib.sha256()
    for rel in CODE_FILES:
        digest.update(rel.encode())
        digest.update((root / rel).read_bytes())
    return digest.hexdigest()[:16]


def load_test_set(bagian: str, folder: Path = TEST_SET_DIR) -> list[dict]:
    """Pesan bagian tertentu beserta klaim emasnya. bagian: dev / heldout / semua."""
    pesan, klaim = load(folder)  # id_klaim & putusan_pesan yang dikosongkan dilengkapi otomatis
    report = validate(pesan, klaim)
    if report.errors:
        raise SystemExit("Test set masih punya error; jalankan `python -m eval.test_set cek`.\n" + report.text())
    if bagian != "semua":
        split_path = folder / "split.csv"
        if not split_path.exists():
            raise SystemExit("split.csv belum ada; jalankan `python -m eval.test_set bagi` dulu.")
        parts = {row["id_pesan"]: row["bagian"] for row in read_csv(split_path)}
        pesan = [row for row in pesan if parts.get(row["id_pesan"]) == bagian]
    claims_of: dict[str, list[dict]] = {}
    for row in klaim:
        claims_of.setdefault(row["id_pesan"], []).append(row)
    return [{**row, "klaim_emas": claims_of.get(row["id_pesan"], [])} for row in pesan]


def run_meta(nama: str, bagian: str, tanpa_critic: bool, jumlah: int, folder: Path = TEST_SET_DIR) -> dict:
    from agents.common.llm import DEFAULT_MODEL
    from agents.orchestrator import CLAIM_TIMEOUT, MAX_KLAIM, MAX_REVISI, TOKEN_BUDGET
    from agents.worker import MAX_STALLED_STEPS, MAX_STEPS

    return {"nama": nama, "bagian": bagian, "critic": not tanpa_critic, "jumlah_pesan": jumlah,
            "model": DEFAULT_MODEL, "sidik_jari_kode": code_fingerprint(),
            "konfigurasi": {"MAX_STEPS": MAX_STEPS, "MAX_STALLED_STEPS": MAX_STALLED_STEPS, "MAX_REVISI": MAX_REVISI,
                            "MAX_KLAIM": MAX_KLAIM, "TOKEN_BUDGET": TOKEN_BUDGET, "CLAIM_TIMEOUT": CLAIM_TIMEOUT},
            "test_set": str(folder), "blocklist": str(folder / "blocklist_eval.txt"),
            "mulai": datetime.now().isoformat(timespec="seconds")}


def check_meta(path: Path, meta: dict) -> None:
    """Melanjutkan run hanya bila sistemnya sama; mencampur dua versi sistem dalam satu run membuat angka tak bermakna."""
    if not path.exists():
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        return
    old = json.loads(path.read_text(encoding="utf-8"))
    for key in ("bagian", "critic", "model", "sidik_jari_kode", "konfigurasi"):
        if old.get(key) != meta.get(key):
            raise SystemExit(f"Run '{meta['nama']}' sudah ada dengan {key} berbeda ({old.get(key)} vs {meta.get(key)}).\n"
                             "Pakai --nama lain untuk run baru, jangan mencampur dua versi sistem dalam satu run.")


async def evaluate_message(client, message: dict, audit: AuditLog) -> dict:
    """Satu pesan -> laporan Orchestrator + label emas + ringkasan trace."""
    from agents.common.a2a_client import artifact_data, status_text
    from agents.common.a2a_server import text_part
    from harness.trace import summary

    correlation_id = new_correlation_id()
    start = time.perf_counter()
    try:
        task = await client.run([text_part(message["teks_pesan"])], correlation_id=correlation_id,
                                timeout=MESSAGE_TIMEOUT, poll_interval=3.0)
        state, report, note = task["status"]["state"], artifact_data(task, "laporan"), status_text(task)
    except Exception as exc:  # Orchestrator mati/tidak terjangkau: dicatat, run tetap lanjut ke pesan berikutnya
        state, report, note = "tidak_terkirim", None, f"{type(exc).__name__}: {exc}"
    trace = summary(audit.read(correlation_id))
    for agent in trace.get("biaya_per_agent", {}).values():  # Counter -> dict agar bisa disimpan sebagai JSON
        agent["tool_call"], agent["status_task"] = dict(agent["tool_call"]), dict(agent["status_task"])
    return {"id_pesan": message["id_pesan"], "kategori": message["kategori"], "teks_pesan": message["teks_pesan"],
            "putusan_emas": message["putusan_pesan"], "klaim_emas": message["klaim_emas"],
            "status_task": state, "catatan_task": note, "laporan": report, "trace": trace,
            "waktu_detik": round(time.perf_counter() - start, 1), "selesai": datetime.now().isoformat(timespec="seconds")}


async def run(messages: list[dict], out: Path, client_factory, audit: AuditLog) -> list[str]:
    """Memproses pesan yang belum punya file hasil. Mengembalikan id pesan yang diproses pada pemanggilan ini."""
    done = []
    async with client_factory() as client:
        for index, message in enumerate(messages, 1):
            path = out / f"{message['id_pesan']}.json"
            if path.exists():
                continue
            print(f"[{index}/{len(messages)}] {message['id_pesan']} ({message['kategori']}) ...", flush=True)
            result = await evaluate_message(client, message, audit)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            putusan = (result["laporan"] or {}).get("putusan", result["status_task"])
            print(f"    -> {putusan} (emas {result['putusan_emas']}), {result['waktu_detik']} dtk, "
                  f"{result['trace'].get('token_total', 0)} token", flush=True)
            done.append(message["id_pesan"])
    return done


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bagian", choices=["dev", "heldout", "semua"], default="dev")
    parser.add_argument("--tanpa-critic", action="store_true")
    parser.add_argument("--nama", help="nama run (default: <bagian>_<critic|tanpa_critic>)")
    parser.add_argument("--batas", type=int, default=0, help="proses maksimal N pesan baru (0 = semua)")
    parser.add_argument("--folder", type=Path, default=TEST_SET_DIR, help="folder test set (default data/test_set)")
    args = parser.parse_args()

    nama = args.nama or f"{args.bagian}_{'tanpa_critic' if args.tanpa_critic else 'critic'}"
    messages = load_test_set(args.bagian, args.folder)
    out = RESULTS / nama
    out.mkdir(parents=True, exist_ok=True)
    check_meta(out / "meta.json", run_meta(nama, args.bagian, args.tanpa_critic, len(messages), args.folder))
    pending = [m for m in messages if not (out / f"{m['id_pesan']}.json").exists()]
    if args.batas:
        pending = pending[:args.batas]
    print(f"run {nama}: {len(messages)} pesan, {len(messages) - len(pending)} sudah selesai, {len(pending)} akan diproses")
    if not pending:
        return

    blocklist = args.folder / "blocklist_eval.txt"
    if not blocklist.exists():
        raise SystemExit(f"{blocklist} belum ada; jalankan `python -m eval.test_set blocklist` dulu.")
    os.environ["SAKSI_BLOCKLIST_FILE"] = str(blocklist)  # diwarisi semua agent dan server MCP-nya

    import run_all
    from agents.common.a2a_client import A2AClient

    processes = run_all.start_all(tanpa_critic=args.tanpa_critic)
    try:
        asyncio.run(run(pending, out, lambda: A2AClient(ORCHESTRATOR_URL, sender="evaluasi", timeout=60), AuditLog()))
    except KeyboardInterrupt:
        print("dihentikan; jalankan perintah yang sama untuk melanjutkan")
    finally:
        run_all.stop_all(processes)
    print(f"hasil di {out}; hitung metrik: python -m eval.score {out}")


if __name__ == "__main__":
    main()
