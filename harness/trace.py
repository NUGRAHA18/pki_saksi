"""Tracing satu pesan dari audit log (Fase 6; Bab 26.2.6 tracing, 26.8 cost attribution).

Semua agent menulis ke logs/audit.jsonl dengan correlation_id yang sama untuk satu pesan pengguna. Modul ini
menyusunnya kembali menjadi:
- timeline: siapa memanggil siapa, kapan, dan hasilnya;
- biaya per agent: token, panggilan LLM, retry dan waktu tunggu backoff, panggilan tool per server MCP, task A2A,
  revisi, dan error.

    python -m harness.trace                 # pesan terakhir di log
    python -m harness.trace 7cdde8abf917    # correlation_id tertentu
    python -m harness.trace --timeline      # sertakan timeline lengkap
"""
from collections import Counter
from datetime import datetime

from agents.common.audit import AuditEvent, AuditLog

AGENTS = ("orchestrator", "worker", "critic")


def last_correlation_id(audit: AuditLog) -> str:
    events = [event for event in audit.read() if event.correlation_id]
    return events[-1].correlation_id if events else ""


def _time(event: AuditEvent) -> datetime:
    return datetime.fromisoformat(event.waktu)


def cost_by_agent(events: list[AuditEvent]) -> dict[str, dict]:
    """Biaya per agent. Token hanya dihitung dari llm_call supaya tidak terhitung dua kali (task_selesai juga
    membawa total token)."""
    def empty() -> dict:
        return {"token": 0, "llm_call": 0, "retry": 0, "tunggu_detik": 0.0, "tool_call": Counter(), "tool_error": 0,
                "task_dikirim": 0, "status_task": Counter(), "revisi": 0, "error": 0, "injeksi_terdeteksi": 0}

    cost: dict[str, dict] = {agent: empty() for agent in AGENTS}
    for event in events:
        agent = cost.setdefault(event.pengirim, empty())  # mis. "pengguna" (klien run_all/UI)
        if event.jenis == "llm_call":
            agent["token"] += event.token
            agent["llm_call"] += 1
            agent["retry"] += max(0, event.percobaan - 1)
            agent["tunggu_detik"] += event.tunggu_detik
        elif event.jenis == "tool_call":
            agent["tool_call"][event.penerima] += 1
            agent["tool_error"] += event.status == "error"
        elif event.jenis == "status_task":
            agent["status_task"][event.status] += 1
        elif event.jenis in {"task_dikirim", "revisi", "error", "injeksi_terdeteksi"}:
            agent[event.jenis] += 1
            if event.jenis == "error":
                agent["token"] += event.token  # token percobaan LLM yang gagal tetap biaya
    for agent in cost.values():
        agent["tunggu_detik"] = round(agent["tunggu_detik"], 1)
    return cost


def summary(events: list[AuditEvent]) -> dict:
    if not events:
        return {"jumlah_kejadian": 0}
    start, end = _time(events[0]), _time(events[-1])
    final = [e for e in events if e.pengirim == "orchestrator" and e.jenis == "task_selesai"]
    cost = cost_by_agent(events)
    return {
        "correlation_id": events[0].correlation_id,
        "durasi_detik": round((end - start).total_seconds(), 1),
        "putusan": final[-1].status if final else "",
        "token_total": sum(agent["token"] for agent in cost.values()),
        # pesan A2A: setiap task yang dikirim (termasuk revisi) dan setiap perubahan status yang dicatat server
        "pesan_a2a": sum(agent["task_dikirim"] + sum(agent["status_task"].values()) for agent in cost.values()),
        "biaya_per_agent": cost,
        "jumlah_kejadian": len(events),
    }


def timeline(events: list[AuditEvent]) -> list[str]:
    if not events:
        return []
    start = _time(events[0])
    lines = []
    for event in events:
        offset = (_time(event) - start).total_seconds()
        extra = []
        if event.token:
            extra.append(f"{event.token} tok")
        if event.percobaan > 1:
            extra.append(f"{event.percobaan} percobaan, tunggu {event.tunggu_detik:.0f}s")
        if event.waktu_ms:
            extra.append(f"{event.waktu_ms / 1000:.1f}s")
        lines.append(f"+{offset:7.1f}s  {event.pengirim:>12} → {event.penerima:<13} {event.jenis:<18} "
                     f"{event.status:<14} {('(' + ', '.join(extra) + ') ') if extra else ''}{event.isi[:90]}")
    return lines


def format_summary(data: dict) -> str:
    if not data.get("jumlah_kejadian"):
        return "Tidak ada kejadian untuk correlation_id ini."
    lines = [f"correlation_id {data['correlation_id']} | putusan {data['putusan'] or '-'} | durasi {data['durasi_detik']} dtk"
             f" | token {data['token_total']} | pesan A2A {data['pesan_a2a']} | {data['jumlah_kejadian']} kejadian", "",
             f"{'agent':<13}{'token':>8}{'LLM':>6}{'retry':>7}{'tunggu':>9}{'tool':>6}{'tool err':>10}{'task A2A':>10}"
             f"{'revisi':>8}{'error':>7}{'injeksi':>9}"]
    for name, agent in data["biaya_per_agent"].items():
        lines.append(f"{name:<13}{agent['token']:>8}{agent['llm_call']:>6}{agent['retry']:>7}"
                     f"{agent['tunggu_detik']:>8.0f}s{sum(agent['tool_call'].values()):>6}{agent['tool_error']:>10}"
                     f"{agent['task_dikirim']:>10}{agent['revisi']:>8}{agent['error']:>7}{agent['injeksi_terdeteksi']:>9}")
    tools = Counter()
    for agent in data["biaya_per_agent"].values():
        tools.update(agent["tool_call"])
    if tools:
        lines += ["", "panggilan tool per server MCP: " + ", ".join(f"{k} {v}" for k, v in tools.most_common())]
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    audit = AuditLog()
    ids = [arg for arg in argv if not arg.startswith("--")]
    correlation_id = ids[0] if ids else last_correlation_id(audit)
    events = audit.read(correlation_id)
    print(format_summary(summary(events)))
    if "--timeline" in argv:
        print("\nTIMELINE")
        print("\n".join(timeline(events)))


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])

