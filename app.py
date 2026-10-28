"""UI SAKSI (Fase 9): tempel pesan, lihat putusan, bukti, dan jejak kerja setiap agent.

UI hanya klien A2A ke Orchestrator; agent dijalankan terpisah:
    .venv\\Scripts\\python.exe run_all.py
    .venv\\Scripts\\streamlit.exe run app.py

Selama pemeriksaan berjalan (bisa beberapa menit), kemajuan dibaca dari audit log (logs/audit.jsonl) berdasarkan
correlation_id pesan ini. Laporan yang selesai disimpan ke results/laporan_<correlation_id>.json dan bisa dibuka lagi
dari bagian "Pemeriksaan sebelumnya" di sidebar (bukan riwayat chat: daftar hasil pemeriksaan yang tersimpan).
"""
import asyncio
from html import escape
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import streamlit as st

from agents.common.a2a_client import A2AClient, artifact_data, status_text
from agents.common.a2a_server import TERMINAL, text_part
from agents.common.audit import AuditEvent, AuditLog, new_correlation_id
from agents.common.config import ROOT, env
from harness.trace import summary

ORCHESTRATOR_URL = env("SAKSI_ORCHESTRATOR_URL", "http://127.0.0.1:8100")
AGENTS = {"Orchestrator": 8100, "Pemeriksa (Worker)": 8101, "Pemeriksa independen (Critic)": 8102}
RESULTS = ROOT / "results"
POLL_SECONDS = 2.0

WARNA = {"FAKTA": "#32d583", "HOAKS": "#f97066", "SEBAGIAN BENAR": "#fdb022", "TIDAK TERVERIFIKASI": "#98a2b3",
         "BUKAN KLAIM FAKTUAL": "#667085"}
IKON_PUTUSAN = {"FAKTA": ":material/check_circle:", "HOAKS": ":material/cancel:", "SEBAGIAN BENAR": ":material/contrast:",
                "TIDAK TERVERIFIKASI": ":material/help:"}
PENJELASAN_PUTUSAN = {
    "FAKTA": "Semua klaim faktual dalam pesan ini didukung sumber.",
    "HOAKS": "Pesan ini memuat klaim yang dibantah sumber, dan tidak ada klaim lain yang terbukti benar.",
    "SEBAGIAN BENAR": "Sebagian klaim terbukti benar, tetapi ada klaim yang dibantah sumber.",
    "TIDAK TERVERIFIKASI": "Belum ada cukup bukti untuk memastikan isi pesan ini. Jangan disebarkan dulu.",
}
TOOL = {"cari_cek_fakta": "mencari arsip cek fakta", "cari_wikipedia": "mencari di Wikipedia",
        "ringkasan_wikipedia": "membaca Wikipedia", "cari_berita": "mencari berita",
        "ambil_halaman": "membaca halaman", "cek_kutipan": "mencocokkan kutipan", "jawab": "menyimpulkan"}
STATUS_KUTIPAN = {"ditemukan_di_halaman": ("kutipan ada di halaman sumber", "#32d583"),
                  "ditemukan_di_hasil_tool": ("kutipan sesuai hasil pencarian sumber", "#a99cff"),
                  "tidak_ditemukan": ("kutipan tidak ditemukan", "#f97066"),
                  "tidak_bisa_dicek": ("halaman tidak bisa dibuka", "#98a2b3")}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap');
html, body, [class*="css"], .stMarkdown, button, input, textarea { font-family: 'Plus Jakarta Sans', sans-serif; }
.block-container { max-width: 860px; padding-top: 3.2rem; padding-bottom: 7rem; }
header[data-testid="stHeader"] { background: transparent; }

/* sambutan */
.hero { text-align: center; margin: clamp(.5rem, 5vh, 3.5rem) 0 1.6rem 0; }
.hero .title { font-size: 2rem; font-weight: 600; letter-spacing: -.02em; line-height: 1.2; }
.hero .sub { color: #9a9aa5; margin: .55rem auto 0 auto; font-size: .95rem; max-width: 620px; line-height: 1.5; }
.hint { font-size: .8rem; color: #7c7c88; text-align: center; margin-top: 1.1rem; }
.features { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: .85rem; }
.feature { position: relative; overflow: hidden; background: #131317; border: 1px solid #26262c; border-radius: 14px;
           padding: 1rem .95rem; min-height: 150px; }
.feature::before { content: ""; position: absolute; inset: 0 0 auto 0; height: 92px;
                   background: radial-gradient(130% 100% at 85% 0%, var(--g) 0%, transparent 72%); opacity: .5; }
.feature svg { position: relative; width: 22px; height: 22px; stroke: var(--a); }
.feature .title { position: relative; margin: .75rem 0 .3rem 0; font-size: .95rem; font-weight: 600; line-height: 1.3; }
.feature p { position: relative; color: #8e8e99; font-size: .78rem; line-height: 1.4; margin: 0; }
/* setelah definisi .feature, supaya tidak tertimpa */
@media (max-width: 1100px) { .features { grid-template-columns: repeat(2, minmax(0, 1fr)); }
                             .feature { min-height: 0; } .feature::before { height: 70px; } }
.st-key-chips { margin-top: 1.4rem; }
.st-key-chips button { border-radius: 999px; background: #131317; border-color: #2a2a31; min-height: 2.4rem; }
.st-key-chips button p { font-size: .84rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.st-key-chips button:hover { border-color: #8b7cf6; }

/* jawaban */
.verdict { border: 1px solid #26262c; border-left: 4px solid var(--c); background: #121216; border-radius: 12px;
           padding: .85rem 1.1rem; margin: .5rem 0 1rem 0; }
.verdict .label { font-size: 1.45rem; font-weight: 700; color: var(--c); letter-spacing: -.01em; }
.verdict .why { color: #c9c9d1; margin-top: .15rem; font-size: .92rem; }
.verdict .meta { color: #7c7c88; font-size: .78rem; margin-top: .45rem; }
.section { font-size: .78rem; font-weight: 600; color: #7c7c88; letter-spacing: .06em; text-transform: uppercase;
           margin: 1.1rem 0 .5rem 0; }
.pill { display: inline-block; font-size: .66rem; font-weight: 700; letter-spacing: .05em; color: var(--c);
        background: color-mix(in srgb, var(--c) 14%, transparent); border: 1px solid color-mix(in srgb, var(--c) 40%, transparent);
        padding: .12rem .55rem; border-radius: 999px; }
.claim-text { font-size: 1.02rem; font-weight: 600; margin: .55rem 0 .3rem 0; line-height: 1.45; }
.summary { color: #c9c9d1; font-size: .9rem; }
.quote { border-left: 2px solid #3a3a44; padding: .1rem .8rem; margin: .75rem 0 .25rem 0; color: #d4d4dc;
         font-style: italic; font-size: .9rem; }
.source { font-size: .76rem; color: #8e8e99; }
.source a { color: #a99cff; text-decoration: none; }
.check { color: var(--c); font-weight: 600; }
.note { font-size: .8rem; color: #8e8e99; }
.progress-line { font-size: .84rem; color: #c9c9d1; padding: .12rem 0; }
[data-testid="stExpander"] details { border-color: #26262c; }
[data-testid="stExpander"] summary p { font-size: .84rem; color: #a3a3ad; }

/* percakapan: pesan pengguna sebagai gelembung di kanan tanpa ikon; jawaban di kiri tanpa latar */
[data-testid="stChatMessage"] { background: transparent; padding: .35rem 0; gap: .75rem; }
/* kedua peran memakai ikon kustom (testid sama), jadi pesan pengguna dikenali dari penanda .from-user */
[data-testid="stChatMessage"]:has(.from-user) { flex-direction: row-reverse; }
[data-testid="stChatMessage"]:has(.from-user) [data-testid^="stChatMessageAvatar"] { display: none; }
[data-testid="stChatMessage"]:has(.from-user) [data-testid="stChatMessageContent"] {
  background: #1f1f25; border-radius: 18px; padding: .7rem 1.05rem; margin-left: 18%; flex: 0 1 auto; }
/* Streamlit memberi margin-bottom -16px pada wadah Markdown: teks menjorok keluar gelembung */
[data-testid="stChatMessage"]:has(.from-user) [data-testid="stMarkdownContainer"] { margin-bottom: 0; }
[data-testid="stChatMessage"]:has(.from-user) [data-testid="stChatMessageContent"] p:last-child { margin-bottom: 0; }
.from-user { display: none; }
[data-testid="stChatMessageContent"] p { margin-bottom: .35rem; }
[data-testid="stChatInput"] { border-radius: 16px; }

/* sidebar */
.brand-card { display: flex; gap: .7rem; align-items: center; background: #1a1a1f; border: 1px solid #26262c;
              border-radius: 12px; padding: .65rem .8rem; margin: 0 0 .9rem 0; }
.logo { width: 34px; height: 34px; border-radius: 9px; background: linear-gradient(135deg, #8b7cf6, #2ec4b6);
        display: grid; place-items: center; font-weight: 700; color: #0b0b0d; font-size: 1.05rem; }
.brand-name { font-weight: 700; font-size: .98rem; line-height: 1.2; }
.brand-sub { font-size: .74rem; color: #8e8e99; }
.side-label { font-size: .72rem; color: #6f6f7a; margin: 1.1rem 0 .45rem .1rem; font-weight: 600; }
.status { font-size: .76rem; color: #8e8e99; line-height: 1.7; }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--c); margin-right: .45rem; }
.st-key-riwayat { gap: .15rem; }
.st-key-riwayat button { justify-content: flex-start; text-align: left; padding: .3rem .55rem; min-height: 2rem;
                         border-radius: 8px; }
.st-key-riwayat button > div { justify-content: flex-start !important; width: 100%; min-width: 0; }
.st-key-riwayat button > div > span { margin: 0 !important; width: 100%; max-width: 100%;
                                      justify-content: flex-start !important; }
.st-key-riwayat button:hover { background: #1a1a1f; }
.st-key-riwayat button p { font-size: .82rem; color: #c9c9d1; white-space: nowrap; overflow: hidden;
                           text-overflow: ellipsis; text-align: left; }
.st-key-ekspor { display: flex; justify-content: flex-end; }
</style>
"""

ICON = {
    "arsip": '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
             '<rect x="3" y="4" width="18" height="5" rx="1.5"/><path d="M5 9v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9"/>'
             '<path d="M10 13h4"/></svg>',
    "sumber": '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
              '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/></svg>',
    "kutipan": '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
               '<path d="M7 17h3l1-5V7H6v5h3"/><path d="M15 17h3l1-5V7h-5v5h3"/></svg>',
    "independen": '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
                  '<path d="M12 3 5 6v5c0 4.5 3 8 7 10 4-2 7-5.5 7-10V6z"/><path d="m9 12 2 2 4-4"/></svg>',
}
FITUR = [
    ("arsip", "Arsip cek fakta", "Dicocokkan dengan artikel Tempo, AFP, Tirto, dan TurnBackHoax.", "#4f8cff"),
    ("sumber", "Wikipedia & berita", "Mencari bukti di Wikipedia dan berita 30 hari terakhir.", "#ff8a3d"),
    ("kutipan", "Kutipan terverifikasi", "Kutipan dicocokkan dengan sumbernya; kutipan karangan ditolak.", "#8b7cf6"),
    ("independen", "Pemeriksa independen", "Hasil ditinjau ulang pemeriksa terpisah sebelum diputus.", "#2ec4b6"),
]
CONTOH = [
    ("BPJS dihapus 1 November?", "katanya bpjs akan di hapus per 1 november. apakah betul?"),
    ("Jakarta di Pulau Sumatra?", "Info dari grup: Jakarta, ibu kota Indonesia, terletak di Pulau Sumatra. BMKG itu "
                                  "lembaga pemerintah nonkementerian. Sebarkan!"),
    ("Borobudur di Magelang?", "Bangga! Candi Borobudur ada di Kabupaten Magelang, Jawa Tengah. Ayo sebarkan ke "
                               "teman-teman!"),
]
AVATAR = {"user": ":material/person:", "assistant": ":material/fact_check:"}


# --- data ------------------------------------------------------------------------------------------------------------

def agent_status() -> dict[str, bool]:
    status = {}
    for name, port in AGENTS.items():
        try:
            status[name] = httpx.get(f"http://127.0.0.1:{port}/.well-known/agent-card.json", timeout=1.0).status_code == 200
        except httpx.HTTPError:
            status[name] = False
    return status


def _client() -> A2AClient:
    return A2AClient(ORCHESTRATOR_URL, sender="pengguna", timeout=30)


async def _send(pesan: str, correlation_id: str) -> dict:
    async with _client() as client:
        return await client.send([text_part(pesan)], correlation_id=correlation_id)


async def _get(task_id: str) -> dict:
    async with _client() as client:
        return await client.get(task_id, history_length=0)


def saved_reports() -> list[Path]:
    return sorted(RESULTS.glob("laporan_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def history_label(path: Path) -> str:
    """'28 Okt 19:09 · HOAKS · Waspada! Vaksin mRNA Covid-19 terbukti…' untuk daftar pemeriksaan sebelumnya."""
    from datetime import datetime

    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"(laporan rusak) {path.stem}"
    months = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
    moment = datetime.fromtimestamp(path.stat().st_mtime)
    text = " ".join(str(report.get("pesan", "")).split())
    snippet = text[:48] + ("…" if len(text) > 48 else "")
    return f"{moment.day} {months[moment.month - 1]} {moment:%H:%M} · {report.get('putusan', '?')} · {snippet}"


def save_report(report: dict) -> Path:
    path = RESULTS / f"laporan_{report['correlation_id']}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# --- teks kemajuan dari audit log ------------------------------------------------------------------------------------

def _argument(isi: str) -> str:
    """'cari_berita {"query": "gempa"}' -> 'gempa' (query, judul, atau domain URL)."""
    match = re.search(r"\{.*\}", isi)
    try:
        args = json.loads(match.group(0)) if match else {}
    except ValueError:
        return ""
    if args.get("url"):
        return urlparse(args["url"]).netloc.removeprefix("www.")
    return str(args.get("query") or args.get("judul") or "")


def progress_lines(events: list[AuditEvent]) -> list[tuple[float, str]]:
    """Kejadian audit log -> kalimat kemajuan yang bisa dibaca pengguna (detik sejak mulai, teks)."""
    if not events:
        return []
    from datetime import datetime

    start = datetime.fromisoformat(events[0].waktu)
    lines = []
    for event in events:
        offset = (datetime.fromisoformat(event.waktu) - start).total_seconds()
        text = ""
        if event.pengirim == "orchestrator" and event.jenis == "llm_call" and event.isi.startswith("ekstraksi klaim:"):
            try:
                claims = json.loads(event.isi.split(":", 1)[1])
                factual = sum(c.get("jenis") == "fakta" for c in claims)
                text = f"Pesan dipecah menjadi {len(claims)} klaim; {factual} klaim faktual akan diperiksa."
            except ValueError:
                text = "Pesan dipecah menjadi beberapa klaim."
        elif event.jenis == "task_dikirim" and event.penerima == "worker":
            text = "Satu klaim diserahkan ke pemeriksa."
        elif event.jenis == "task_dikirim" and event.penerima == "critic":
            text = "Hasil pemeriksaan diserahkan ke pemeriksa independen."
        elif event.pengirim == "worker" and event.jenis == "tool_call":
            tool = event.isi.split(" ", 1)[0]
            target = _argument(event.isi)
            text = f"Pemeriksa {TOOL.get(tool, tool)}" + (f": {target}" if target else "") + (
                " (gagal, mencoba sumber lain)" if event.status == "error" else "")
        elif event.pengirim == "critic" and event.jenis == "penilaian":
            verdict = "menerima" if event.status == "TERIMA" else "menolak"
            reason = re.sub(r"^\[\w+\] ", "", event.isi)  # buang penanda "[llm]" / "[aturan]"
            text = f"Pemeriksa independen {verdict} hasil: {reason[:160]}"
        elif event.jenis == "revisi":
            text = "Hasil dikembalikan ke pemeriksa untuk diperbaiki."
        elif event.jenis == "injeksi_terdeteksi":
            text = "Sebuah halaman memuat teks yang mencoba memberi perintah; teks itu diabaikan."
        elif event.jenis == "llm_call" and event.percobaan > 1:
            text = f"Menunggu antrean layanan ({event.tunggu_detik:.0f} detik)."
        elif event.jenis == "error":
            text = "Ada kendala pada salah satu langkah; pemeriksaan dilanjutkan."
        if text:
            lines.append((offset, text))
    return lines


def plain_reason(alasan: str) -> str:
    """'aturan 2: 1 klaim hoaks dan 1 klaim fakta' -> '1 klaim hoaks dan 1 klaim fakta'."""
    return re.sub(r"^aturan \d+:\s*", "", alasan)


# --- tampilan --------------------------------------------------------------------------------------------------------

def pill(label: str) -> str:
    return f'<span class="pill" style="--c:{WARNA.get(label, "#98a2b3")}">{escape(label)}</span>'


def render_verdict(report: dict) -> None:
    label = report.get("putusan", "TIDAK TERVERIFIKASI")
    factual = [c for c in report.get("klaim", []) if c.get("putusan") != "BUKAN KLAIM FAKTUAL"]
    minutes = report.get("waktu_detik", 0) / 60
    st.markdown(
        f'<div class="verdict" style="--c:{WARNA.get(label, "#98a2b3")}">'
        f'<div class="label">{escape(label)}</div>'
        f'<div class="why">{escape(PENJELASAN_PUTUSAN.get(label, ""))}</div>'
        f'<div class="meta">{len(factual)} klaim faktual diperiksa · {escape(plain_reason(report.get("alasan_putusan", "")))}'
        f' · {minutes:.1f} menit</div></div>', unsafe_allow_html=True)


def _checks_by_url(claim: dict) -> dict[str, dict]:
    checks = (claim.get("penilaian_critic") or {}).get("pemeriksaan_kutipan", [])
    return {check.get("url", ""): check for check in checks}


def render_claim(claim: dict) -> None:
    """Satu kartu per klaim; jejak pemeriksaan berada di dalam kartu yang sama."""
    label = claim.get("putusan", "")
    with st.container(border=True):
        parts = [pill(label), f'<div class="claim-text">{escape(claim.get("teks", ""))}</div>']
        if label == "BUKAN KLAIM FAKTUAL":
            parts.append(f'<div class="note">Tidak diperiksa karena berupa {escape(claim.get("jenis", "opini"))}.</div>')
            st.markdown("".join(parts), unsafe_allow_html=True)
            return
        if claim.get("ringkasan"):
            parts.append(f'<div class="summary">{escape(claim["ringkasan"])}</div>')
        checks = _checks_by_url(claim)
        for item in claim.get("bukti", []):
            url = item.get("url", "")
            domain = urlparse(url).netloc.removeprefix("www.")
            check = checks.get(url)
            badge = ""
            if check and check.get("status") in STATUS_KUTIPAN:
                text, color = STATUS_KUTIPAN[check["status"]]
                badge = f' · <span class="check" style="--c:{color}">{text}</span>'
            parts.append(f'<div class="quote">“{escape(item.get("kutipan", ""))}”</div>'
                         f'<div class="source">{escape(item.get("sumber") or domain)} · '
                         f'<a href="{escape(url)}" target="_blank">{escape(domain)}</a>{badge}</div>')
        if not claim.get("bukti") and label == "TIDAK TERVERIFIKASI":
            parts.append('<div class="note" style="margin-top:.4rem">Tidak ditemukan sumber yang membahas klaim ini.</div>')
        rounds = [r for r in claim.get("putaran", []) if r.get("critic")]
        if rounds:
            revised = f", setelah {claim.get('revisi', 0)} kali perbaikan" if claim.get("revisi") else ""
            verdict = "menerima" if rounds[-1]["critic"] == "TERIMA" else "menolak"
            parts.append(f'<div class="note" style="margin-top:.7rem">Pemeriksa independen {verdict} hasil ini{revised}.</div>')
        st.markdown("".join(parts), unsafe_allow_html=True)

        with st.expander("Jejak pemeriksaan"):
            for step in claim.get("langkah", []):
                tool = TOOL.get(step.get("tool", ""), step.get("tool", ""))
                args = step.get("argumen", {})
                target = args.get("query") or args.get("judul") or args.get("url") or ""
                st.markdown(f"**{step.get('nomor')}. {tool.capitalize()}**" + (f" — `{target}`" if target else ""))
                if step.get("alasan"):
                    st.caption(f"Pertimbangan: {step['alasan']}")
                if step.get("tool") != "jawab" and step.get("observasi"):
                    st.caption(f"Hasil: {step['observasi'][:240]}")
            for item in claim.get("putaran", []):
                if item.get("critic"):
                    st.caption(f"Putaran {item['putaran']}: pemeriksa menyimpulkan {item.get('sikap')}, "
                               f"pemeriksa independen {item['critic']} — {item.get('alasan_critic', '')}")
            if claim.get("catatan"):
                st.caption("Catatan teknis: " + " · ".join(claim["catatan"]))


def render_agents(report: dict) -> None:
    correlation_id = report.get("correlation_id", "")
    data = summary(AuditLog().read(correlation_id))
    if data.get("jumlah_kejadian"):
        names = {"orchestrator": "Orchestrator", "worker": "Pemeriksa (Worker)", "critic": "Pemeriksa independen (Critic)"}
        rows = []
        for key, name in names.items():
            cost = data["biaya_per_agent"].get(key, {})
            rows.append({"Agent": name, "Token": cost.get("token", 0), "Panggilan model": cost.get("llm_call", 0),
                         "Sumber dibuka": sum(cost.get("tool_call", {}).values()),
                         "Task dikirim": cost.get("task_dikirim", 0), "Tunggu antrean (dtk)": cost.get("tunggu_detik", 0)})
        st.dataframe(rows, hide_index=True, width="stretch")
        st.caption(f"{data['pesan_a2a']} pesan antar-agent · {data['token_total']} token · {data['durasi_detik']} detik "
                   f"· correlation_id {correlation_id}")
        from harness.trace import timeline

        st.code("\n".join(timeline(AuditLog().read(correlation_id))), language=None)
    else:
        st.caption("Jejak antar-agent tidak tersedia untuk laporan ini.")
    for note in report.get("catatan", []):
        st.caption(f"Catatan teknis: {note}")


def render_report(report: dict) -> None:
    label = report.get("putusan", "TIDAK TERVERIFIKASI")
    st.markdown(f"**{label}.** {PENJELASAN_PUTUSAN.get(label, '')}")
    render_verdict(report)
    claims = report.get("klaim", [])
    if claims:
        st.markdown('<div class="section">Klaim dalam pesan</div>', unsafe_allow_html=True)
        for claim in claims:
            render_claim(claim)
    else:
        st.markdown('<div class="note">Pesan tidak bisa dipecah menjadi klaim.</div>', unsafe_allow_html=True)
    with st.expander("Kerja setiap agent"):
        render_agents(report)


def check(pesan: str) -> tuple[dict | None, str]:
    """Kirim pesan ke Orchestrator dan tampilkan kemajuan di dalam gelembung jawaban. (laporan, pesan error)."""
    correlation_id = new_correlation_id()
    try:
        task = asyncio.run(_send(pesan, correlation_id))
    except Exception as exc:
        return None, f"Pesan tidak bisa dikirim ke Orchestrator: {exc}"
    start, shown = time.monotonic(), 0
    with st.status("Memeriksa pesan…", expanded=True) as box:
        while task["status"]["state"] not in TERMINAL | {"input-required"}:
            lines = progress_lines(AuditLog().read(correlation_id))
            for _, text in lines[shown:]:
                st.markdown(f'<div class="progress-line">{escape(text)}</div>', unsafe_allow_html=True)
            shown = len(lines)
            box.update(label=f"Memeriksa pesan… {time.monotonic() - start:.0f} detik")
            time.sleep(POLL_SECONDS)
            task = asyncio.run(_get(task["id"]))
        box.update(label=f"Selesai diperiksa dalam {time.monotonic() - start:.0f} detik", state="complete",
                   expanded=False)
    report = artifact_data(task, "laporan")
    if not report:
        return None, f"Pemeriksaan berakhir dengan status {task['status']['state']}: {status_text(task)}"
    save_report(report)
    return report, ""


def export_markdown(chat: list[dict]) -> str:
    """Percakapan sebagai Markdown untuk diunduh (pesan, putusan, klaim, bukti)."""
    lines = ["# Percakapan SAKSI", ""]
    for turn in chat:
        if turn["role"] == "user":
            lines += [f"**Pesan:** {turn['text']}", ""]
        elif turn.get("report"):
            report = turn["report"]
            lines += [f"**Putusan: {report.get('putusan')}** — {plain_reason(report.get('alasan_putusan', ''))}", ""]
            for claim in report.get("klaim", []):
                lines.append(f"- [{claim.get('putusan')}] {claim.get('teks')}")
                for item in claim.get("bukti", []):
                    lines.append(f"  - \"{item.get('kutipan', '')}\" — {item.get('url', '')}")
            lines += ["", "---", ""]
        else:
            lines += [turn.get("text", ""), ""]
    return "\n".join(lines)


def render_turn(turn: dict) -> None:
    with st.chat_message(turn["role"], avatar=AVATAR[turn["role"]]):
        if turn["role"] == "user":
            st.markdown('<span class="from-user"></span>' + escape(turn["text"]).replace("\n", "  \n"),
                        unsafe_allow_html=True)
        elif turn.get("report"):
            render_report(turn["report"])
        else:
            st.markdown(turn.get("text", ""))


def welcome() -> None:
    st.markdown('<div class="hero"><div class="title">Selamat datang di SAKSI.</div><div class="sub">Tempel pesan berantai '
                'yang ingin kamu periksa. Setiap klaim dicek ke beberapa sumber dan dijawab lengkap dengan kutipannya.'
                '</div></div>', unsafe_allow_html=True)
    cards = "".join(f'<div class="feature" style="--g:{color};--a:{color}">{ICON[icon]}<div class="title">{title}</div>'
                    f'<p>{text}</p></div>' for icon, title, text, color in FITUR)
    st.markdown(f'<div class="features">{cards}</div>', unsafe_allow_html=True)  # satu grid: tinggi kartu seragam
    with st.container(key="chips"):
        for index, (column, (short, full)) in enumerate(zip(st.columns(3), CONTOH)):
            if column.button(short, key=f"contoh_{index}", help=full, width="stretch"):
                st.session_state["pending"] = full
                st.rerun()
    st.markdown('<div class="hint">Setiap pesan diperiksa terpisah; pesan sebelumnya tidak ikut diperhitungkan. '
                'Satu pemeriksaan biasanya 1–6 menit.</div>', unsafe_allow_html=True)


def _history_groups(paths: list[Path]) -> list[tuple[str, list[Path]]]:
    from datetime import date, datetime, timedelta

    today = date.today()
    groups: dict[str, list[Path]] = {"Hari ini": [], "Kemarin": [], "Sebelumnya": []}
    for path in paths:
        day = datetime.fromtimestamp(path.stat().st_mtime).date()
        key = "Hari ini" if day == today else "Kemarin" if day == today - timedelta(days=1) else "Sebelumnya"
        groups[key].append(path)
    return [(name, items) for name, items in groups.items() if items]


def sidebar(chat: list[dict]) -> dict[str, bool]:
    with st.sidebar:
        st.markdown('<div class="brand-card"><div class="logo">S</div><div><div class="brand-name">SAKSI</div>'
                    '<div class="brand-sub">Pemeriksa pesan berantai</div></div></div>', unsafe_allow_html=True)
        if st.button("Percakapan baru", icon=":material/edit_square:", width="stretch"):
            st.session_state["chat"] = []
            st.rerun()
        status = agent_status()
        st.markdown('<div class="side-label">Status agent</div>', unsafe_allow_html=True)
        st.markdown("".join(f'<div class="status"><span class="dot" style="--c:{"#32d583" if ok else "#f97066"}"></span>'
                            f'{escape(name)} — {"aktif" if ok else "tidak aktif"}</div>' for name, ok in status.items()),
                    unsafe_allow_html=True)
        if not all(status.values()):
            st.caption("Jalankan `python run_all.py` di terminal lain.")
        with st.container(key="riwayat"):
            for group, paths in _history_groups(saved_reports()[:15]):
                st.markdown(f'<div class="side-label">{group}</div>', unsafe_allow_html=True)
                for path in paths:
                    try:
                        report = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    text = " ".join(str(report.get("pesan", "")).split()) or path.stem
                    if st.button(text[:40], key=f"riwayat_{path.stem}", help=history_label(path), type="tertiary",
                                 icon=IKON_PUTUSAN.get(report.get("putusan"), ":material/chat_bubble:"), width="stretch"):
                        chat += [{"role": "user", "text": report.get("pesan", "")}, {"role": "assistant", "report": report}]
                        st.rerun()
    return status


def main() -> None:
    st.set_page_config(page_title="SAKSI · Periksa Pesan", page_icon=":material/fact_check:", layout="centered")
    st.markdown(CSS, unsafe_allow_html=True)
    chat: list[dict] = st.session_state.setdefault("chat", [])
    # ?laporan=<correlation_id>: buka laporan tersimpan langsung (tautan untuk demo/presentasi)
    requested = st.query_params.get("laporan")
    if requested and not st.session_state.get("_dibuka_dari_url"):
        st.session_state["_dibuka_dari_url"] = True
        path = RESULTS / f"laporan_{re.sub(r'[^0-9a-f]', '', requested)}.json"
        if path.exists():
            report = json.loads(path.read_text(encoding="utf-8"))
            chat += [{"role": "user", "text": report.get("pesan", "")}, {"role": "assistant", "report": report}]

    status = sidebar(chat)
    if chat:
        with st.container(key="ekspor"):
            st.download_button("Ekspor percakapan", export_markdown(chat), file_name="percakapan_saksi.md",
                               mime="text/markdown", icon=":material/ios_share:", type="secondary")
    else:
        welcome()
    for turn in chat:
        render_turn(turn)

    prompt = st.chat_input("Tempel pesan berantai yang ingin diperiksa…") or st.session_state.pop("pending", None)
    if not prompt or not prompt.strip():
        return
    pesan = prompt.strip()
    chat.append({"role": "user", "text": pesan})
    render_turn(chat[-1])
    with st.chat_message("assistant", avatar=AVATAR["assistant"]):
        if not all(status.values()):
            reply = {"role": "assistant", "text": "Belum semua agent aktif, jadi pesan ini belum bisa diperiksa. "
                                                  "Jalankan `python run_all.py` di terminal lain, lalu kirim ulang."}
        else:
            report, error = check(pesan)
            reply = {"role": "assistant", "report": report} if report else {"role": "assistant", "text": error}
    chat.append(reply)
    st.rerun()


if __name__ == "__main__" or st.runtime.exists():
    main()
