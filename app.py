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

WARNA = {"FAKTA": "#067647", "HOAKS": "#b42318", "SEBAGIAN BENAR": "#b54708", "TIDAK TERVERIFIKASI": "#475467",
         "BUKAN KLAIM FAKTUAL": "#98a2b3"}
PENJELASAN_PUTUSAN = {
    "FAKTA": "Semua klaim faktual dalam pesan ini didukung sumber.",
    "HOAKS": "Pesan ini memuat klaim yang dibantah sumber, dan tidak ada klaim lain yang terbukti benar.",
    "SEBAGIAN BENAR": "Sebagian klaim terbukti benar, tetapi ada klaim yang dibantah sumber.",
    "TIDAK TERVERIFIKASI": "Belum ada cukup bukti untuk memastikan isi pesan ini. Jangan disebarkan dulu.",
}
TOOL = {"cari_cek_fakta": "mencari arsip cek fakta", "cari_wikipedia": "mencari di Wikipedia",
        "ringkasan_wikipedia": "membaca Wikipedia", "cari_berita": "mencari berita",
        "ambil_halaman": "membaca halaman", "cek_kutipan": "mencocokkan kutipan", "jawab": "menyimpulkan"}
STATUS_KUTIPAN = {"ditemukan_di_halaman": ("kutipan ada di halaman sumber", "#067647"),
                  "ditemukan_di_hasil_tool": ("kutipan sesuai hasil pencarian sumber", "#1d3557"),
                  "tidak_ditemukan": ("kutipan tidak ditemukan", "#b42318"),
                  "tidak_bisa_dicek": ("halaman tidak bisa dibuka", "#98a2b3")}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,500;8..60,600&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
html, body, [class*="css"], .stMarkdown, .stTextArea textarea, button { font-family: 'IBM Plex Sans', sans-serif; }
h1, h2, h3, .serif { font-family: 'Source Serif 4', Georgia, serif !important; letter-spacing: -0.01em; }
.block-container { max-width: 860px; padding-top: 2.2rem; }
.masthead { border-bottom: 2px solid #1f2328; padding-bottom: .4rem; margin-bottom: 1.2rem; }
.masthead .name { font-family: 'Source Serif 4', Georgia, serif; font-size: 2.1rem; font-weight: 600; }
.masthead .tag { color: #57606a; font-size: .95rem; }
.verdict { border-left: 6px solid var(--c); background: #fff; padding: 1rem 1.2rem; margin: .6rem 0 1.4rem 0;
           border-radius: 2px; box-shadow: 0 1px 0 #e7e3d8; }
.verdict .label { font-family: 'Source Serif 4', Georgia, serif; font-size: 1.9rem; font-weight: 600; color: var(--c); }
.verdict .why { color: #31363b; margin-top: .2rem; }
.verdict .meta { color: #6e7781; font-size: .85rem; margin-top: .5rem; }
.pill { display: inline-block; font-size: .72rem; font-weight: 600; letter-spacing: .04em; color: #fff;
        background: var(--c); padding: .12rem .5rem; border-radius: 2px; vertical-align: middle; }
.claim { background: #fff; border: 1px solid #e7e3d8; padding: .9rem 1.1rem; margin-bottom: .9rem; border-radius: 2px; }
.claim .text { font-family: 'Source Serif 4', Georgia, serif; font-size: 1.15rem; margin: .35rem 0 .4rem 0; }
.claim .summary { color: #31363b; }
.quote { border-left: 3px solid #d0cbbd; padding: .15rem .8rem; margin: .6rem 0 .2rem 0; color: #31363b; font-style: italic; }
.source { font-size: .82rem; color: #57606a; }
.source a { color: #1d3557; }
.check { font-size: .75rem; font-weight: 600; color: var(--c); }
.note { font-size: .82rem; color: #6e7781; }
.progress-line { font-size: .9rem; color: #31363b; padding: .12rem 0; border-bottom: 1px dotted #e7e3d8; }
.progress-line .t { color: #8c959f; font-variant-numeric: tabular-nums; display: inline-block; width: 4.2rem; }
</style>
"""


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
    return f'<span class="pill" style="--c:{WARNA.get(label, "#475467")}">{escape(label)}</span>'


def render_verdict(report: dict) -> None:
    label = report.get("putusan", "TIDAK TERVERIFIKASI")
    factual = [c for c in report.get("klaim", []) if c.get("putusan") != "BUKAN KLAIM FAKTUAL"]
    minutes = report.get("waktu_detik", 0) / 60
    st.markdown(
        f'<div class="verdict" style="--c:{WARNA.get(label, "#475467")}">'
        f'<div class="label">{escape(label)}</div>'
        f'<div class="why">{escape(PENJELASAN_PUTUSAN.get(label, ""))}</div>'
        f'<div class="meta">{len(factual)} klaim faktual diperiksa · {escape(plain_reason(report.get("alasan_putusan", "")))}'
        f' · {minutes:.1f} menit</div></div>', unsafe_allow_html=True)


def _checks_by_url(claim: dict) -> dict[str, dict]:
    checks = (claim.get("penilaian_critic") or {}).get("pemeriksaan_kutipan", [])
    return {check.get("url", ""): check for check in checks}


def render_claim(claim: dict) -> None:
    label = claim.get("putusan", "")
    parts = [f'<div class="claim">{pill(label)}<div class="text">{escape(claim.get("teks", ""))}</div>']
    if label == "BUKAN KLAIM FAKTUAL":
        parts.append(f'<div class="note">Tidak diperiksa karena berupa {escape(claim.get("jenis", "opini"))}.</div></div>')
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
        parts.append('<div class="note">Tidak ditemukan sumber yang membahas klaim ini.</div>')
    rounds = [r for r in claim.get("putaran", []) if r.get("critic")]
    if rounds:
        last = rounds[-1]
        revised = f", setelah {claim.get('revisi', 0)} kali perbaikan" if claim.get("revisi") else ""
        verdict = "menerima" if last["critic"] == "TERIMA" else "menolak"
        parts.append(f'<div class="note" style="margin-top:.6rem">Pemeriksa independen {verdict} hasil ini{revised}.</div>')
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)

    with st.expander("Jejak pemeriksaan klaim ini"):
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


def render_agents(correlation_id: str) -> None:
    data = summary(AuditLog().read(correlation_id))
    if not data.get("jumlah_kejadian"):
        st.caption("Jejak antar-agent tidak tersedia untuk laporan ini.")
        return
    names = {"orchestrator": "Orchestrator", "worker": "Pemeriksa (Worker)", "critic": "Pemeriksa independen (Critic)"}
    rows = []
    for key, name in names.items():
        cost = data["biaya_per_agent"].get(key, {})
        rows.append({"Agent": name, "Token": cost.get("token", 0), "Panggilan model": cost.get("llm_call", 0),
                     "Sumber dibuka": sum(cost.get("tool_call", {}).values()), "Task dikirim": cost.get("task_dikirim", 0),
                     "Tunggu antrean (dtk)": cost.get("tunggu_detik", 0)})
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption(f"{data['pesan_a2a']} pesan antar-agent · {data['token_total']} token · {data['durasi_detik']} detik · "
               f"correlation_id {correlation_id}")
    from harness.trace import timeline

    with st.expander("Log lengkap antar-agent"):
        st.code("\n".join(timeline(AuditLog().read(correlation_id))), language=None)


def render_report(report: dict) -> None:
    render_verdict(report)
    st.markdown('<h3 class="serif">Klaim dalam pesan</h3>', unsafe_allow_html=True)
    for claim in report.get("klaim", []):
        render_claim(claim)
    if not report.get("klaim"):
        st.markdown('<div class="note">Pesan tidak bisa dipecah menjadi klaim.</div>', unsafe_allow_html=True)
    for note in report.get("catatan", []):
        st.caption(f"Catatan teknis: {note}")
    st.markdown('<h3 class="serif">Kerja setiap agent</h3>', unsafe_allow_html=True)
    render_agents(report.get("correlation_id", ""))


def render_progress(placeholder, correlation_id: str, elapsed: float) -> None:
    lines = progress_lines(AuditLog().read(correlation_id))
    html = "".join(f'<div class="progress-line"><span class="t">{offset:5.0f} dtk</span>{escape(text)}</div>'
                   for offset, text in lines[-14:])
    placeholder.markdown(f'<div class="note">Sedang diperiksa · {elapsed:.0f} detik berjalan. Pemeriksaan satu pesan '
                         f'biasanya beberapa menit.</div>{html}', unsafe_allow_html=True)


def check(pesan: str) -> dict | None:
    correlation_id = new_correlation_id()
    try:
        task = asyncio.run(_send(pesan, correlation_id))
    except Exception as exc:
        st.error(f"Pesan tidak bisa dikirim ke Orchestrator: {exc}")
        return None
    placeholder, start = st.empty(), time.monotonic()
    while task["status"]["state"] not in TERMINAL | {"input-required"}:
        render_progress(placeholder, correlation_id, time.monotonic() - start)
        time.sleep(POLL_SECONDS)
        task = asyncio.run(_get(task["id"]))
    placeholder.empty()
    report = artifact_data(task, "laporan")
    if not report:
        st.error(f"Pemeriksaan berakhir dengan status {task['status']['state']}: {status_text(task)}")
        return None
    save_report(report)
    return report


def main() -> None:
    st.set_page_config(page_title="SAKSI · Periksa Pesan", layout="centered")
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown('<div class="masthead"><div class="name">SAKSI</div><div class="tag">Periksa pesan berantai sebelum '
                'ikut menyebarkannya. Setiap klaim diperiksa terhadap arsip cek fakta, Wikipedia, dan berita, lalu '
                'ditinjau ulang oleh pemeriksa independen.</div></div>', unsafe_allow_html=True)

    with st.sidebar:
        st.markdown('<div class="serif" style="font-size:1.15rem;font-weight:600">Status agent</div>',
                    unsafe_allow_html=True)
        status = agent_status()
        for name, ok in status.items():
            st.markdown(f'<div class="note">{"●" if ok else "○"} {escape(name)} — {"aktif" if ok else "tidak aktif"}</div>',
                        unsafe_allow_html=True)
        if not all(status.values()):
            st.caption("Jalankan `python run_all.py` di terminal lain.")
        st.markdown('<div class="serif" style="font-size:1.15rem;font-weight:600;margin-top:1.2rem">'
                    'Pemeriksaan sebelumnya</div>', unsafe_allow_html=True)
        st.caption("Hasil pemeriksaan yang tersimpan. Membukanya tidak memeriksa ulang.")
        reports = saved_reports()
        chosen = st.selectbox("Buka hasil pemeriksaan sebelumnya", [None] + reports, label_visibility="collapsed",
                              format_func=lambda p: "Pilih…" if p is None else history_label(p))
        if chosen is not None:
            st.session_state["report"] = json.loads(chosen.read_text(encoding="utf-8"))

    pesan = st.text_area("Tempel pesan yang ingin diperiksa", height=150,
                         placeholder="Contoh: Info dari grup sebelah, mulai bulan depan denda tilang naik 150 persen…")
    if st.button("Periksa pesan", type="primary", disabled=not pesan.strip()):
        if not all(status.values()):
            st.warning("Belum semua agent aktif. Jalankan `python run_all.py` lalu coba lagi.")
        else:
            report = check(pesan.strip())
            if report:
                st.session_state["report"] = report

    report = st.session_state.get("report")
    if report:
        st.markdown(f'<div class="note" style="margin-top:1rem">Pesan yang diperiksa:</div>'
                    f'<div class="quote" style="font-style:normal">{escape(report.get("pesan", ""))}</div>',
                    unsafe_allow_html=True)
        render_report(report)


if __name__ == "__main__" or st.runtime.exists():
    main()
