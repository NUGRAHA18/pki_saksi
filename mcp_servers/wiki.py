"""mcp-wiki: Wikipedia bahasa Indonesia dan Inggris (tanpa API key).

Dua tool: cari judul halaman, lalu ambil ringkasan (paragraf awal) satu halaman. Wikipedia meminta User-Agent yang
menyebut identitas pemanggil, jadi dipakai USER_AGENT dari common.

Jalankan sebagai server MCP (stdio):  python -m mcp_servers.wiki
"""
from datetime import timedelta
import re
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_servers.common import cache_get, cache_put, get_json

CACHE_AGE = timedelta(days=7)
BAHASA = {"id", "en"}

server = MCPServer(
    "mcp-wiki",
    instructions="Mencari dan membaca ringkasan halaman Wikipedia (id/en) sebagai bukti latar belakang.",
)


def _check_language(bahasa: str) -> str:
    if bahasa not in BAHASA:
        raise ToolError(f"bahasa harus salah satu dari {sorted(BAHASA)}")
    return bahasa


@server.tool()
def cari_wikipedia(query: str, bahasa: str = "id", maks: int = 5) -> dict:
    """Cari halaman Wikipedia yang relevan dengan query.

    query: nama tokoh, lembaga, peristiwa, atau istilah (mis. "Badan Meteorologi Klimatologi dan Geofisika").
    bahasa: "id" atau "en".
    maks: jumlah hasil maksimum (1-10).
    Hasil: judul dan cuplikan; pakai ringkasan_wikipedia(judul) untuk membaca isinya.
    """
    _check_language(bahasa)
    maks = max(1, min(int(maks), 10))
    key = {"op": "cari", "query": query, "bahasa": bahasa, "maks": maks}
    data = cache_get("wiki", key, CACHE_AGE)
    from_cache = data is not None
    if data is None:
        raw = get_json(f"https://{bahasa}.wikipedia.org/w/api.php", {
            "action": "query", "list": "search", "srsearch": query, "srlimit": maks, "format": "json",
        })
        data = {"hasil": [
            {"judul": item["title"], "cuplikan": re.sub(r"<[^>]+>", "", item.get("snippet", "")),
             "url": f"https://{bahasa}.wikipedia.org/wiki/{quote(item['title'].replace(' ', '_'))}"}
            for item in raw.get("query", {}).get("search", [])
        ]}
        cache_put("wiki", key, data)
    return {"query": query, "bahasa": bahasa, "jumlah": len(data["hasil"]), **data, "dari_cache": from_cache}


@server.tool()
def ringkasan_wikipedia(judul: str, bahasa: str = "id") -> dict:
    """Ambil ringkasan (paragraf pembuka) satu halaman Wikipedia berdasarkan judul persisnya.

    judul: judul halaman, sebaiknya dari hasil cari_wikipedia.
    bahasa: "id" atau "en".
    """
    _check_language(bahasa)
    key = {"op": "ringkasan", "judul": judul, "bahasa": bahasa}
    data = cache_get("wiki", key, CACHE_AGE)
    from_cache = data is not None
    if data is None:
        title = quote(judul.strip().replace(" ", "_"), safe="")
        raw = get_json(f"https://{bahasa}.wikipedia.org/api/rest_v1/page/summary/{title}", {})
        data = {
            "judul": raw.get("title", judul),
            "deskripsi": raw.get("description", ""),
            "ringkasan": raw.get("extract", ""),
            "url": raw.get("content_urls", {}).get("desktop", {}).get("page", ""),
            "halaman_disambiguasi": raw.get("type") == "disambiguation",
        }
        cache_put("wiki", key, data)
    return {**data, "dari_cache": from_cache}


if __name__ == "__main__":
    server.run()
