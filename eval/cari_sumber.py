"""Bantuan mengumpulkan test set: cari artikel cek fakta dan berita untuk sebuah kata kunci (Fase 7).

Memakai server MCP SAKSI sendiri (Google Fact Check Tools + GNews). Hasilnya kandidat yang siap disalin ke CSV;
tetap BUKA dan BACA artikelnya sebelum memakai labelnya.

    python -m eval.cari_sumber bansos
    python -m eval.cari_sumber "token listrik gratis" --berita    # sertakan berita 30 hari terakhir (memakai kuota GNews)
"""
import asyncio
import sys

from agents.common.mcp_client import MCPHub
from mcp_servers import berita, cekfakta

# rating cek fakta -> label klaim di klaim.csv (rating lain: baca artikelnya dan putuskan sendiri)
RATING_KE_LABEL = {"salah": "HOAKS", "keliru": "HOAKS", "hoaks": "HOAKS", "false": "HOAKS", "penipuan": "HOAKS",
                   "benar": "FAKTA", "true": "FAKTA"}


def suggested_label(rating: str) -> str:
    lowered = rating.lower()
    for key, label in RATING_KE_LABEL.items():
        if key in lowered:
            return label
    return "? (baca artikelnya)"


async def search(query: str, with_news: bool) -> None:
    async with MCPHub({"cekfakta": cekfakta.server, "berita": berita.server}) as hub:
        result = await hub.call("cari_cek_fakta", {"query": query, "maks": 10})
        print(f"\nARTIKEL CEK FAKTA untuk \"{query}\" (kandidat pesan hoaks; sudah_dicek_media = ya)")
        if "error" in result:
            print(f"  error: {result['error']}")
        for item in result.get("hasil", []):
            print(f"\n  klaim     : {item['klaim']}")
            print(f"  rating    : {item['rating']} ({item['penerbit']}) -> usul label {suggested_label(item['rating'])}")
            print(f"  sumber    : {item['url']}")
        if with_news:
            news = await hub.call("cari_berita", {"query": query, "maks": 5})
            print(f"\nBERITA 30 HARI TERAKHIR (kandidat klaim FAKTA; sudah_dicek_media = tidak)")
            if "error" in news:
                print(f"  error: {news['error']}")
            for item in news.get("hasil", []):
                print(f"\n  judul     : {item['judul']}  [{item['sumber']}, {item['terbit']}]")
                print(f"  cuplikan  : {item['cuplikan']}")
                print(f"  sumber    : {item['url']}")
            print(f"\nsisa kuota GNews hari ini: {news.get('sisa_kuota_hari_ini', '?')}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    words = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    if not words:
        print(__doc__)
        raise SystemExit(1)
    asyncio.run(search(" ".join(words), "--berita" in sys.argv))
