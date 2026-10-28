"""mcp-cekfakta: Google Fact Check Tools API (claims:search).

Mencari artikel cek fakta yang sudah pernah diterbitkan (Tempo, Kompas, Liputan6, turnbackhoax, AFP, dll.) untuk
sebuah klaim. Butuh FACTCHECK_API_KEY berupa API key Google biasa; s

Jalankan sebagai server MCP (stdio):  python -m mcp_servers.cekfakta
"""
from datetime import timedelta

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agents.common.config import env
from mcp_servers.common import blocklist, cache_get, cache_put, get_json, is_blocked

API_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
CACHE_AGE = timedelta(days=7)

server = MCPServer(
    "mcp-cekfakta",
    instructions="Mencari artikel cek fakta yang sudah ada untuk sebuah klaim (Google Fact Check Tools API).",
)


@server.tool()
def cari_cek_fakta(query: str, bahasa: str = "id", maks: int = 5) -> dict:
    """Cari artikel cek fakta yang sudah terbit untuk sebuah klaim.

    query: kata kunci inti klaim, singkat (mis. "vaksin mRNA kanker"), bukan kalimat panjang.
    bahasa: kode bahasa artikel, "id" atau "en"; kosongkan ("") untuk semua bahasa.
    maks: jumlah hasil maksimum (1-10).
    Hasil: daftar klaim beserta penerbit, rating (mis. "Salah", "Keliru", "False"), dan URL artikelnya.
    """
    query = query.strip()
    if not query:
        raise ToolError("query kosong")
    maks = max(1, min(int(maks), 10))
    key = {"query": query, "bahasa": bahasa, "maks": maks}

    raw = cache_get("cekfakta", key, CACHE_AGE)
    from_cache = raw is not None
    if raw is None:
        api_key = env("FACTCHECK_API_KEY")
        if not api_key:
            raise ToolError("FACTCHECK_API_KEY belum diisi di .env")
        params = {"query": query, "pageSize": maks, "key": api_key}
        if bahasa:
            params["languageCode"] = bahasa
        raw = get_json(API_URL, params)
        cache_put("cekfakta", key, raw)

    blocked = blocklist()
    hasil, disaring = [], 0
    for claim in raw.get("claims", []):
        for review in claim.get("claimReview", []):
            url = review.get("url", "")
            if is_blocked(url, blocked):
                disaring += 1
                continue
            hasil.append({
                "klaim": claim.get("text", ""),
                "pengklaim": claim.get("claimant", ""),
                "tanggal_klaim": claim.get("claimDate", "")[:10],
                "penerbit": review.get("publisher", {}).get("name") or review.get("publisher", {}).get("site", ""),
                "judul": review.get("title", ""),
                "rating": review.get("textualRating", ""),
                "tanggal_cek": review.get("reviewDate", "")[:10],
                "url": url,
            })
    return {"query": query, "jumlah": len(hasil), "hasil": hasil[:maks], "disaring_blocklist": disaring,
            "dari_cache": from_cache}


if __name__ == "__main__":
    server.run()
