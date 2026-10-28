"""mcp-berita: pencarian berita lewat GNews

Paket gratis: 100 request/hari, maks 10 artikel/request, arsip 30 hari, isi artikel hanya cuplikan (teks lengkap
dibaca lewat mcp-fetch). Kuota dihitung lokal (common.use_quota); hit cache tidak dihitung.

Jalankan sebagai server MCP (stdio):  python -m mcp_servers.berita
"""
from datetime import date, timedelta

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agents.common.config import env
from mcp_servers.common import blocklist, cache_get, cache_put, get_json, is_blocked, use_quota

API_URL = "https://gnews.io/api/v4/search"
CACHE_AGE = timedelta(days=1)  # berita cepat berubah, cache lebih pendek daripada cek fakta/wiki
DAILY_LIMIT = int(env("GNEWS_DAILY_LIMIT", "100"))
ARCHIVE_DAYS = 29  # arsip paket gratis 30 hari; disisakan 1 hari supaya tidak terpotong di batasnya

server = MCPServer(
    "mcp-berita",
    instructions="Mencari artikel berita 30 hari terakhir (GNews). Isi artikel hanya cuplikan; baca lewat mcp-fetch.",
)


@server.tool()
def cari_berita(query: str, bahasa: str = "id", negara: str = "id", maks: int = 5, dari: str = "",
                sampai: str = "") -> dict:
    """Cari artikel berita (maksimal 30 hari ke belakang) yang membahas sebuah klaim atau peristiwa.

    query: kata kunci singkat; frasa persis diberi tanda kutip, mis. "\\"gempa Cianjur\\"". Operator AND/OR/NOT boleh.
    bahasa: "id" atau "en". negara: "id" untuk media Indonesia, kosongkan ("") untuk semua negara.
    maks: jumlah artikel (1-10). dari/sampai: tanggal ISO opsional, mis. "2026-10-01".
    Hasil: judul, cuplikan, sumber, tanggal terbit, dan URL. Klaim lebih tua dari 30 hari tidak akan ditemukan.
    """
    query = query.strip()
    if not query:
        raise ToolError("query kosong")
    maks = max(1, min(int(maks), 10))
    params = {"q": query, "lang": bahasa, "max": maks, "sortby": "relevance"}
    if negara:
        params["country"] = negara
    # Paket gratis membuang artikel > 30 hari SETELAH diurutkan menurut relevansi, sehingga hasil bisa kosong walau
    # totalArticles ratusan. Karena itu pencarian selalu dibatasi ke jendela 30 hari (dibulatkan per hari agar cache
    # tetap kena dalam satu hari).
    earliest = (date.today() - timedelta(days=ARCHIVE_DAYS)).isoformat()
    params["from"] = f"{max(dari[:10], earliest) if dari else earliest}T00:00:00Z"
    if sampai:
        params["to"] = sampai if "T" in sampai else f"{sampai}T23:59:59Z"

    raw = cache_get("berita", params, CACHE_AGE)
    from_cache = raw is not None
    sisa_kuota = None
    if raw is None:
        api_key = env("NEWS_API_KEY")
        if not api_key:
            raise ToolError("NEWS_API_KEY belum diisi di .env")
        sisa_kuota = use_quota("gnews", DAILY_LIMIT)
        raw = get_json(API_URL, {**params, "apikey": api_key})
        cache_put("berita", params, raw)

    blocked = blocklist()
    hasil, disaring = [], 0
    for article in raw.get("articles", []):
        if is_blocked(article.get("url", ""), blocked):
            disaring += 1
            continue
        hasil.append({
            "judul": article.get("title", ""),
            "cuplikan": article.get("description", ""),
            "sumber": article.get("source", {}).get("name", ""),
            "terbit": article.get("publishedAt", "")[:10],
            "url": article.get("url", ""),
        })
    removed = raw.get("articlesRemovedFromResponse") or {}
    return {"query": query, "rentang": f"{params['from'][:10]} s.d. {params.get('to', 'sekarang')[:10]}",
            "total_di_gnews": raw.get("totalArticles", 0), "jumlah": len(hasil), "hasil": hasil,
            "dibuang_gnews": [item.get("message", "") for item in removed.values()],
            "disaring_blocklist": disaring, "dari_cache": from_cache, "sisa_kuota_hari_ini": sisa_kuota}


if __name__ == "__main__":
    server.run()
