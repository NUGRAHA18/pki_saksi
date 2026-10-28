"""MCP client SAKSI: menyambung satu agent ke beberapa server MCP sekaligus (Bab 23).

Setiap server berjalan sebagai proses sendiri lewat stdio. Agent hanya diberi server yang ia butuhkan (least privilege,
Bab 25.8.3): Worker -> semua, Critic -> fetch saja.

    async with MCPHub(["wiki", "fetch"]) as hub:
        tools = hub.tools()                       # deskripsi tool untuk function calling LLM (Fase 2)
        hasil = await hub.call("cari_wikipedia", {"query": "BMKG"})

Uji langsung keempat server (memakai kuota API sungguhan; hasil dicache):
    python -m agents.common.mcp_client
"""
from contextlib import AsyncExitStack
import json
import os
import sys
import time
from typing import Any

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters, get_default_environment

from .audit import AuditEvent, AuditLog
from .config import ROOT

SERVERS = {
    "cekfakta": "mcp_servers.cekfakta",
    "wiki": "mcp_servers.wiki",
    "berita": "mcp_servers.berita",
    "fetch": "mcp_servers.fetch",
}


def server_params(name: str) -> StdioServerParameters:
    # stdio hanya mewarisi sebagian kecil env; variabel SAKSI_* (mis. SAKSI_BLOCKLIST) diteruskan eksplisit
    extra = {key: value for key, value in os.environ.items() if key.startswith("SAKSI_")}
    return StdioServerParameters(command=sys.executable, args=["-m", SERVERS[name]], cwd=str(ROOT),
                                 env={**get_default_environment(), **extra, "PYTHONIOENCODING": "utf-8"})


class MCPHub:
    def __init__(self, servers: list[str] | dict[str, Any], agent: str = "", audit: AuditLog | None = None,
                 timeout: float = 60) -> None:
        """servers: daftar nama dari SERVERS (dijalankan lewat stdio), atau {nama: target Client} untuk tes in-memory."""
        self._targets = servers if isinstance(servers, dict) else {name: server_params(name) for name in servers}
        self.agent = agent
        self.audit = audit
        self.timeout = timeout
        self._stack = AsyncExitStack()
        self._clients: dict[str, Client] = {}
        self._tools: list = []
        self._owner: dict[str, str] = {}  # nama tool -> nama server

    async def __aenter__(self) -> "MCPHub":
        for name, target in self._targets.items():
            client = await self._stack.enter_async_context(Client(target, read_timeout_seconds=self.timeout))
            self._clients[name] = client
            for tool in (await client.list_tools()).tools:
                if tool.name in self._owner:
                    raise ValueError(f"tool {tool.name} ada di dua server: {self._owner[tool.name]} dan {name}")
                self._owner[tool.name] = name
                self._tools.append(tool)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    def tools(self) -> list:
        return list(self._tools)

    def server_of(self, tool_name: str) -> str:
        return self._owner[tool_name]

    async def call(self, tool_name: str, arguments: dict, correlation_id: str = "") -> dict:
        """Memanggil tool; selalu mengembalikan dict. Kegagalan tool menjadi {"error": pesan}, bukan exception,
        supaya loop agent bisa membaca error sebagai Observation dan memilih langkah lain."""
        if tool_name not in self._owner:
            return {"error": f"tool tidak dikenal: {tool_name}"}
        server = self._owner[tool_name]
        start = time.perf_counter()
        try:
            result = await self._clients[server].call_tool(tool_name, arguments)
            text = "".join(getattr(part, "text", "") for part in result.content)
            if result.is_error:
                output = {"error": text}
            elif result.structured_content is not None:
                output = result.structured_content
            else:
                try:
                    output = json.loads(text)
                except ValueError:
                    output = {"teks": text}
        except Exception as exc:  # proses server mati / timeout: jangan sampai menjatuhkan agent
            output = {"error": f"{type(exc).__name__}: {exc}"}
        if self.audit:
            self.audit.log(AuditEvent(
                correlation_id, self.agent, f"mcp-{server}", "tool_call", isi=f"{tool_name} {json.dumps(arguments, ensure_ascii=False)}",
                status="error" if "error" in output else "ok", waktu_ms=round((time.perf_counter() - start) * 1000, 1),
            ))
        return output


async def _demo() -> None:
    from mcp_servers.common import quota_used

    samples = [
        ("cari_cek_fakta", {"query": "vaksin mRNA kanker", "maks": 3}),
        ("cari_wikipedia", {"query": "Badan Meteorologi Klimatologi dan Geofisika", "maks": 3}),
        ("ringkasan_wikipedia", {"judul": "Badan Meteorologi, Klimatologi, dan Geofisika"}),
        ("cari_berita", {"query": "BMKG gempa", "maks": 3}),
        ("ambil_halaman", {"url": "https://id.wikipedia.org/wiki/Badan_Meteorologi,_Klimatologi,_dan_Geofisika",
                           "maks_karakter": 600}),
        ("cek_kutipan", {"url": "https://id.wikipedia.org/wiki/Badan_Meteorologi,_Klimatologi,_dan_Geofisika",
                         "kutipan": "Badan Meteorologi, Klimatologi, dan Geofisika"}),
        ("ringkasan_wikipedia", {"judul": "x", "bahasa": "jp"}),  # error yang disengaja
    ]
    async with MCPHub(list(SERVERS)) as hub:
        print(f"{len(hub.tools())} tool dari {len(SERVERS)} server:")
        for tool in hub.tools():
            print(f"  [{hub.server_of(tool.name)}] {tool.name}: {tool.description.splitlines()[0]}")
        for name, args in samples:
            start = time.perf_counter()
            output = await hub.call(name, args)
            print(f"\n== {name} {args}  ({time.perf_counter() - start:.1f} dtk)")
            print(json.dumps(output, ensure_ascii=False, indent=1)[:900])
    print(f"\nKuota GNews terpakai hari ini (hitungan lokal): {quota_used('gnews')}")


if __name__ == "__main__":
    import anyio

    anyio.run(_demo)
