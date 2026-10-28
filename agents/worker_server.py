"""Worker sebagai server A2A (Fase 3): setiap task = satu klaim yang diperiksa Worker (agents/worker.py).

Masukan (message parts): data part {"klaim": "...", "feedback": "...", "anggaran_token": N} atau text part berisi
klaim. anggaran_token opsional: bila tercapai, Worker menjawab dengan bukti yang sudah ada.
Keluaran: artifact "hasil_pemeriksaan" (data part = WorkerResult.to_dict()).
Bila klaim kosong, task berstatus input-required dan menunggu klaim dikirim pada taskId yang sama (multi-turn).
Bila LLM gagal, task berstatus failed dengan artifact berisi TIDAK_CUKUP, supaya Orchestrator tetap punya hasil.

Server MCP (stdio) dan klien LLM dibuka sekali saat server hidup lalu dipakai bersama oleh semua task paralel.

Jalankan:  python -m agents.worker_server --port 8101
"""
from contextlib import asynccontextmanager

from .common.a2a_server import (A2AServer, Completed, Failed, InputRequired, TaskContext, agent_card, data_part,
                                make_artifact, serve)
from .common.audit import AuditLog
from .common.config import env
from .common.llm import ToolCallingLLM
from .common.mcp_client import MCPHub, SERVERS
from .worker import Worker

ARTIFACT = "hasil_pemeriksaan"
DEFAULT_PORT = 8101

SKILL = {
    "id": "periksa_klaim",
    "name": "Periksa satu klaim",
    "description": "Mencari bukti untuk satu klaim faktual (cek fakta, Wikipedia, berita, isi halaman) lalu memberi "
                   "sikap MENDUKUNG/MEMBANTAH/TIDAK_CUKUP beserta kutipan dan URL sumber.",
    "tags": ["cek fakta", "hoaks", "bukti"],
    "examples": ['{"klaim": "Jakarta terletak di Pulau Sumatra"}'],
    "inputModes": ["application/json", "text/plain"],
    "outputModes": ["application/json"],
}


def make_handler(llm: ToolCallingLLM, hub_ref: dict, audit: AuditLog | None):
    async def handler(ctx: TaskContext) -> Completed | InputRequired | Failed:
        data = ctx.data()
        klaim = str(data.get("klaim") or ctx.text()).strip()
        if not klaim:
            return InputRequired("Klaim kosong. Kirim klaim yang akan diperiksa pada task ini "
                                 '(text, atau data {"klaim": "..."}).')
        worker = Worker(llm, hub_ref["hub"], audit=audit)  # satu instans Worker per task/klaim
        budget = data.get("anggaran_token")
        result = await worker.check(klaim, feedback=str(data.get("feedback", "")), correlation_id=ctx.correlation_id,
                                    token_budget=int(budget) if budget else None)
        artifact = make_artifact(ARTIFACT, [data_part(result.to_dict())], "Sikap, bukti, dan jejak ReAct Worker")
        if result.status == "error":
            return Failed("; ".join(result.catatan) or "Worker gagal", [artifact])
        return Completed([artifact], f"{result.sikap} (keyakinan {result.keyakinan})")
    return handler


def build_server(llm: ToolCallingLLM, hub: MCPHub | None = None, port: int = DEFAULT_PORT,
                 audit: AuditLog | None = None) -> A2AServer:
    """hub=None: server MCP dijalankan (stdio) saat server A2A hidup. Tes memberi hub in-memory yang sudah terbuka."""
    hub_ref = {"hub": hub}

    @asynccontextmanager
    async def lifespan(app):
        if hub is not None:
            yield
            return
        async with MCPHub(list(SERVERS), agent="worker", audit=audit) as opened:
            hub_ref["hub"] = opened
            yield

    token = env("SAKSI_A2A_TOKEN")
    card = agent_card("worker", "Pemeriksa fakta: memeriksa satu klaim dengan loop ReAct dan tool MCP.",
                      f"http://127.0.0.1:{port}/", [SKILL], secured=bool(token))
    return A2AServer(card, make_handler(llm, hub_ref, audit), audit=audit, token=token, lifespan=lifespan)


if __name__ == "__main__":
    import argparse

    from .common.llm import GeminiLLM

    parser = argparse.ArgumentParser(description="Worker SAKSI sebagai server A2A")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    print(f"Worker A2A di http://127.0.0.1:{args.port}/  (Agent Card: /.well-known/agent-card.json)")
    serve(build_server(GeminiLLM(), port=args.port, audit=AuditLog()), args.port)
