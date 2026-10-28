"""Menjalankan semua agent SAKSI sebagai proses terpisah (server A2A), lalu menunggu sampai Ctrl+C.

Setiap agent adalah proses sendiri; server MCP dijalankan oleh Worker (keempatnya) dan Critic (mcp-fetch saja), lewat stdio. Urutan: Worker dan Critic dulu, karena
Orchestrator membaca Agent Card Worker dan Critic saat mulai.

    python run_all.py                       # jalankan semua, tunggu Ctrl+C
    python run_all.py --pesan "teks pesan"  # jalankan, periksa satu pesan, cetak laporan, lalu matikan
"""
import argparse
import asyncio
import json
import subprocess
import sys
import time

import httpx

from agents.common.a2a_client import A2AClient, artifact_data
from agents.common.a2a_server import text_part
from agents.common.config import ROOT
from agents.common.audit import new_correlation_id

AGENTS = [  # (nama, modul, port)
    ("worker", "agents.worker_server", 8101),
    ("critic", "agents.critic", 8102),
    ("orchestrator", "agents.orchestrator", 8100),
]


def wait_ready(name: str, port: int, process: subprocess.Popen, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{name} berhenti saat mulai (kode {process.returncode})")
        try:
            httpx.get(f"http://127.0.0.1:{port}/.well-known/agent-card.json", timeout=2).raise_for_status()
            return
        except httpx.HTTPError:
            time.sleep(0.5)
    raise RuntimeError(f"{name} belum siap setelah {timeout:.0f} detik")


def start_all(tanpa_critic: bool = False) -> list[subprocess.Popen]:
    """tanpa_critic: Critic tidak dijalankan dan Orchestrator diberi --tanpa-critic (pembanding eksperimen)."""
    processes = []
    try:
        for name, module, port in AGENTS:
            if tanpa_critic and name == "critic":
                continue
            extra = ["--tanpa-critic"] if tanpa_critic and name == "orchestrator" else []
            process = subprocess.Popen([sys.executable, "-m", module, "--port", str(port), *extra], cwd=ROOT)
            processes.append(process)
            wait_ready(name, port, process)
            print(f"[run_all] {name} siap di http://127.0.0.1:{port}/")
    except Exception:
        stop_all(processes)
        raise
    return processes


def stop_all(processes: list[subprocess.Popen]) -> None:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


async def check(pesan: str) -> None:
    from agents.orchestrator import format_report

    async with A2AClient("http://127.0.0.1:8100", sender="pengguna", timeout=60) as orchestrator:
        correlation_id = new_correlation_id()
        print(f"[run_all] mengirim pesan ke orchestrator (correlation_id {correlation_id}) ...")
        task = await orchestrator.run([text_part(pesan)], correlation_id=correlation_id, timeout=4000,
                                      poll_interval=2.0)
        report = artifact_data(task, "laporan")
        print(f"[run_all] task orchestrator: {task['status']['state']}")
        if report:  # disimpan dulu: laporan tetap ada walau pencetakan ke konsol bermasalah
            path = ROOT / "results" / f"laporan_{correlation_id}.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[run_all] laporan lengkap: {path}\n")
        print(format_report(report) if report else task)


def main() -> None:
    # konsol Windows (cp1252) gagal mencetak karakter non-Latin yang kadang muncul di teks model
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pesan", help="periksa satu pesan lalu matikan semua agent")
    args = parser.parse_args()
    processes = start_all()
    try:
        if args.pesan:
            asyncio.run(check(args.pesan))
        else:
            print("[run_all] semua agent berjalan. Ctrl+C untuk berhenti.")
            while all(process.poll() is None for process in processes):
                time.sleep(1)
            print("[run_all] ada agent yang berhenti; mematikan semuanya.")
    except KeyboardInterrupt:
        pass
    finally:
        stop_all(processes)


if __name__ == "__main__":
    main()
