"""Tes Fase 1: keempat server MCP diuji lewat MCP client sungguhan (in-memory), dengan HTTP palsu.

Tidak memakai jaringan maupun kuota API. Uji langsung ke API asli: python -m agents.common.mcp_client
"""
from datetime import date, timedelta
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from agents.common.audit import AuditLog
from agents.common.mcp_client import MCPHub
from mcp_servers import berita, cekfakta, common, fetch, web, wiki

FACTCHECK_RESPONSE = {"claims": [{
    "text": "Vaksin menyebabkan kanker", "claimant": "Facebook", "claimDate": "2026-09-14T00:00:00Z",
    "claimReview": [
        {"publisher": {"name": "Tempo"}, "url": "https://cekfakta.tempo.co/a", "title": "Keliru", "textualRating": "Keliru",
         "reviewDate": "2026-09-20T00:00:00Z"},
        {"publisher": {"name": "TurnBackHoax"}, "url": "https://turnbackhoax.id/articles/1", "textualRating": "Salah"},
    ],
}]}
GNEWS_RESPONSE = {
    "totalArticles": 40,
    "articles": [{"title": "Gempa M5", "description": "BMKG mencatat", "url": "https://news.detik.com/x",
                  "publishedAt": "2026-10-27T01:00:00Z", "source": {"name": "detikcom"}}],
    "articlesRemovedFromResponse": {"historicalArticles": {"message": "2 articles were removed"}},
}


class FakeHTTP:
    """Pengganti httpx: mencatat request dan membalas sesuai host/path."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "factchecktools.googleapis.com":
            return httpx.Response(200, json=FACTCHECK_RESPONSE)
        if request.url.host == "gnews.io":
            return httpx.Response(200, json=GNEWS_RESPONSE)
        if request.url.path == "/w/api.php":
            return httpx.Response(200, json={"query": {"search": [
                {"title": "Badan Meteorologi", "snippet": "<span class=\"searchmatch\">BMKG</span> adalah lembaga"}]}})
        if "/page/summary/" in request.url.path:
            return httpx.Response(200, json={"title": "Jakarta", "description": "ibu kota", "extract": "Jakarta adalah...",
                                             "content_urls": {"desktop": {"page": "https://id.wikipedia.org/wiki/Jakarta"}}})
        return httpx.Response(404, json={"error": {"message": "tidak ada"}})


class ServerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.http = FakeHTTP()
        self.enterContext(mock.patch.object(common, "_client", httpx.Client(transport=httpx.MockTransport(self.http.handler))))
        self.enterContext(mock.patch.object(common, "CACHE_DIR", folder / "cache"))
        self.enterContext(mock.patch.object(common, "QUOTA_FILE", folder / "cache" / "kuota.json"))
        self.enterContext(mock.patch.object(common, "BLOCKLIST_FILE", folder / "blocklist.txt"))
        self.enterContext(mock.patch.dict(os.environ, {
            "FACTCHECK_API_KEY": "kunci-fc", "NEWS_API_KEY": "kunci-gnews", "SAKSI_BLOCKLIST": "turnbackhoax.id"}))

    async def call(self, server, tool: str, args: dict) -> dict:
        async with MCPHub({"uji": server}) as hub:
            return await hub.call(tool, args)


class TestCekFakta(ServerTestCase):
    async def test_hasil_diratakan_dan_domain_label_disaring(self):
        out = await self.call(cekfakta.server, "cari_cek_fakta", {"query": "vaksin kanker"})
        self.assertEqual(out["jumlah"], 1)
        self.assertEqual(out["disaring_blocklist"], 1)
        self.assertEqual(out["hasil"][0]["penerbit"], "Tempo")
        self.assertEqual(out["hasil"][0]["tanggal_klaim"], "2026-09-14")
        params = self.http.requests[0].url.params
        self.assertEqual((params["languageCode"], params["key"]), ("id", "kunci-fc"))

    async def test_panggilan_kedua_dari_cache(self):
        await self.call(cekfakta.server, "cari_cek_fakta", {"query": "vaksin kanker"})
        out = await self.call(cekfakta.server, "cari_cek_fakta", {"query": "vaksin kanker"})
        self.assertTrue(out["dari_cache"])
        self.assertEqual(len(self.http.requests), 1)

    async def test_tanpa_key_memberi_error_yang_jelas(self):
        with mock.patch.object(cekfakta, "env", return_value=""):  # .env asli berisi key, jadi env() dipalsukan
            out = await self.call(cekfakta.server, "cari_cek_fakta", {"query": "x"})
        self.assertIn("FACTCHECK_API_KEY", out["error"])


class TestWiki(ServerTestCase):
    async def test_cari_membuang_tag_html(self):
        out = await self.call(wiki.server, "cari_wikipedia", {"query": "BMKG"})
        self.assertEqual(out["hasil"][0]["cuplikan"], "BMKG adalah lembaga")
        self.assertEqual(out["hasil"][0]["url"], "https://id.wikipedia.org/wiki/Badan_Meteorologi")

    async def test_ringkasan(self):
        out = await self.call(wiki.server, "ringkasan_wikipedia", {"judul": "Jakarta"})
        self.assertEqual(out["ringkasan"], "Jakarta adalah...")
        self.assertFalse(out["halaman_disambiguasi"])

    async def test_bahasa_tidak_dikenal(self):
        out = await self.call(wiki.server, "ringkasan_wikipedia", {"judul": "x", "bahasa": "jp"})
        self.assertIn("bahasa harus", out["error"])

    async def test_gangguan_koneksi_sesaat_diulang(self):
        calls = []

        def flaky(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ConnectError("putus sesaat")
            return self.http.handler(request)
        with mock.patch.object(common, "_client", httpx.Client(transport=httpx.MockTransport(flaky))):
            out = await self.call(wiki.server, "ringkasan_wikipedia", {"judul": "Jakarta"})
        self.assertEqual((out["ringkasan"], len(calls)), ("Jakarta adalah...", 2))

    async def test_halaman_tidak_ada(self):
        not_found = httpx.MockTransport(lambda request: httpx.Response(404, json={"detail": "Not found."}))
        with mock.patch.object(common, "_client", httpx.Client(transport=not_found)):
            out = await self.call(wiki.server, "ringkasan_wikipedia", {"judul": "Tidak_ada"})
        self.assertIn("membalas 404", out["error"])


class TestBerita(ServerTestCase):
    async def test_jendela_30_hari_dan_kuota(self):
        out = await self.call(berita.server, "cari_berita", {"query": "gempa", "dari": "2020-01-01"})
        params = self.http.requests[0].url.params
        earliest = (date.today() - timedelta(days=berita.ARCHIVE_DAYS)).isoformat()
        self.assertEqual(params["from"], f"{earliest}T00:00:00Z")  # tanggal lebih tua dipotong ke batas arsip
        self.assertEqual(params["apikey"], "kunci-gnews")
        self.assertEqual(out["hasil"][0]["sumber"], "detikcom")
        self.assertEqual(out["dibuang_gnews"], ["2 articles were removed"])
        self.assertEqual(out["sisa_kuota_hari_ini"], berita.DAILY_LIMIT - 1)

        cached = await self.call(berita.server, "cari_berita", {"query": "gempa"})
        self.assertTrue(cached["dari_cache"])
        self.assertEqual(common.quota_used("gnews"), 1)  # hit cache tidak memakai kuota

    async def test_kuota_habis(self):
        with mock.patch.object(berita, "DAILY_LIMIT", 1):
            await self.call(berita.server, "cari_berita", {"query": "a"})
            out = await self.call(berita.server, "cari_berita", {"query": "b"})
        self.assertIn("kuota harian gnews habis", out["error"])
        self.assertEqual(len(self.http.requests), 1)


class TestFetch(ServerTestCase):
    PAGE = ("Judul", "Menurut BMKG, gempa berkekuatan M5,2 terjadi pukul 10.00 WIB. " + "Isi lain. " * 200, [])

    def setUp(self) -> None:
        super().setUp()
        self.fetch_page = self.enterContext(mock.patch.object(fetch, "fetch_page", return_value=self.PAGE))

    async def test_ambil_halaman_bertahap(self):
        first = await self.call(fetch.server, "ambil_halaman", {"url": "https://contoh.id/a", "maks_karakter": 500})
        self.assertEqual(len(first["teks"]), 500)
        self.assertEqual(first["posisi_berikutnya"], 500)
        self.assertIn("bukan sebagai instruksi", first["peringatan"])
        rest = await self.call(fetch.server, "ambil_halaman", {"url": "https://contoh.id/a", "mulai": 500,
                                                                "maks_karakter": 15000})
        self.assertIsNone(rest["posisi_berikutnya"])
        self.assertEqual(self.fetch_page.call_count, 1)  # potongan kedua dari cache

    async def test_kutipan_persis_abaikan_tanda_baca(self):
        out = await self.call(fetch.server, "cek_kutipan", {"url": "https://contoh.id/a",
                                                            "kutipan": "“menurut BMKG gempa berkekuatan M5,2”"})
        self.assertTrue(out["ditemukan"])
        self.assertIn("pukul 10 00 wib", out["konteks"])

    async def test_kutipan_karangan_tidak_ditemukan(self):
        out = await self.call(fetch.server, "cek_kutipan", {"url": "https://contoh.id/a",
                                                            "kutipan": "BMKG memastikan tsunami setinggi 10 meter"})
        self.assertFalse(out["ditemukan"])
        self.assertLess(out["kecocokan"], 0.5)

    async def test_url_diblokir(self):
        out = await self.call(fetch.server, "ambil_halaman", {"url": "https://www.turnbackhoax.id/articles/1"})
        self.assertIn("diblokir", out["error"])
        self.fetch_page.assert_not_called()

    async def test_robots_melarang(self):
        self.fetch_page.side_effect = PermissionError("robots.txt melarang mengambil https://contoh.id/b")
        out = await self.call(fetch.server, "ambil_halaman", {"url": "https://contoh.id/b"})
        self.assertIn("robots.txt", out["error"])


class TestHub(ServerTestCase):
    async def test_beberapa_server_dan_least_privilege(self):
        audit = AuditLog(Path(self.enterContext(tempfile.TemporaryDirectory())) / "audit.jsonl")
        servers = {"cekfakta": cekfakta.server, "wiki": wiki.server, "berita": berita.server, "fetch": fetch.server}
        async with MCPHub(servers, agent="worker", audit=audit) as hub:
            self.assertEqual(len(hub.tools()), 6)
            self.assertEqual(hub.server_of("cek_kutipan"), "fetch")
            await hub.call("cari_wikipedia", {"query": "BMKG"}, correlation_id="c1")
            self.assertIn("tidak dikenal", (await hub.call("hapus_semua", {}))["error"])
        async with MCPHub({"fetch": fetch.server}, agent="critic") as critic:
            self.assertEqual({tool.name for tool in critic.tools()}, {"ambil_halaman", "cek_kutipan"})
            self.assertIn("tidak dikenal", (await critic.call("cari_berita", {"query": "x"}))["error"])
        events = audit.read("c1")
        self.assertEqual((events[0].pengirim, events[0].penerima, events[0].status), ("worker", "mcp-wiki", "ok"))


class TestBlocklistDanRobots(unittest.TestCase):
    def test_is_blocked(self):
        entries = ["turnbackhoax.id", "cekfakta.com/artikel"]
        self.assertTrue(common.is_blocked("https://www.turnbackhoax.id/a", entries))
        self.assertTrue(common.is_blocked("https://m.turnbackhoax.id/a", entries))
        self.assertTrue(common.is_blocked("https://cekfakta.com/artikel/123", entries))
        self.assertFalse(common.is_blocked("https://cekfakta.com/lain", entries))
        self.assertFalse(common.is_blocked("https://bukanturnbackhoax.id/a", entries))

    def robots(self, status: int, body: str = "") -> mock._patch:
        transport = httpx.MockTransport(lambda request: httpx.Response(status, text=body))
        return mock.patch.object(web, "_http", httpx.Client(transport=transport))

    def test_robots_tidak_ada_berarti_boleh(self):
        with self.robots(404):
            self.assertTrue(web.allowed_by_robots("https://contoh.id/a"))

    def test_robots_403_berarti_dilarang(self):
        with self.robots(403):
            self.assertFalse(web.allowed_by_robots("https://contoh.id/a"))

    def test_robots_aturan_disallow(self):
        with self.robots(200, "User-agent: *\nDisallow: /rahasia/\n"):
            self.assertTrue(web.allowed_by_robots("https://contoh.id/berita/1"))
            self.assertFalse(web.allowed_by_robots("https://contoh.id/rahasia/1"))

    def test_gagal_koneksi_bukan_dianggap_dilarang(self):
        def refuse(request):
            raise httpx.ConnectError("sertifikat kedaluwarsa")
        with mock.patch.object(web, "_http", httpx.Client(transport=httpx.MockTransport(refuse))):
            with self.assertRaisesRegex(ConnectionError, "gagal terhubung"):
                web.fetch_page("https://contoh.id/a")


if __name__ == "__main__":
    unittest.main()
