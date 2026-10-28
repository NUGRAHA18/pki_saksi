"""Pengambil halaman web untuk mcp-fetch (Fase 1).

Diambil dari SIAGA (siaga/src/scraper.py): parser HTML tanpa script/style dan cek robots.txt.
Bagian crawl/snapshot tidak dibawa karena SAKSI hanya membaca satu URL bukti per panggilan.

Memakai httpx, bukan urllib: urllib di Windows memakai penyimpanan sertifikat sistem dan gagal verifikasi SSL di
sebagian situs (mis. Wikipedia: "certificate has expired"), sedangkan httpx memakai bundle certifi.
"""
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from agents.common.config import user_agent

USER_AGENT = user_agent()
_http = httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
# selain kode, buang navigasi/menu situs: isinya sama di setiap halaman dan menghabiskan token agent
SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg"}


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.links: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "a" and self._skip_depth == 0:
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += " ".join(data.split())
        elif not self._skip_depth:
            text = " ".join(data.split())
            if text:
                self.parts.append(text)


def parse_html(html: str, url: str) -> tuple[str, str, list[str]]:
    """Mengembalikan (judul, teks bersih, tautan absolut)."""
    parser = _PageParser()
    parser.feed(html)
    links = [urljoin(url, link) for link in parser.links]
    return parser.title, " ".join(parser.parts), links


def allowed_by_robots(url: str, user_agent: str = USER_AGENT) -> bool:
    """Gagal jaringan dilempar sebagai httpx.HTTPError, bukan dianggap "dilarang", supaya pesannya tidak menyesatkan.

    robots.txt diambil sendiri dengan User-Agent SAKSI: RobotFileParser.read() memakai User-Agent bawaan Python yang
    diblokir banyak situs (403), lalu menganggap semua halaman dilarang.
    """
    parsed = urlparse(url)
    response = _http.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt", headers={"User-Agent": user_agent})
    if response.status_code in (401, 403):
        return False
    if response.status_code != 200:
        return True  # tidak ada robots.txt (404 dll.) berarti tidak ada larangan
    parser = robotparser.RobotFileParser()
    parser.parse(response.text.splitlines())
    return parser.can_fetch(user_agent, url)


def fetch_page(url: str, user_agent: str = USER_AGENT) -> tuple[str, str, list[str]]:
    """Mengembalikan (judul, teks, tautan). PermissionError bila robots.txt melarang, ValueError bila halaman tidak
    bisa dipakai (status bukan 200 / bukan HTML), ConnectionError bila gagal terhubung."""
    try:
        if not allowed_by_robots(url, user_agent):
            raise PermissionError(f"robots.txt melarang mengambil {url}")
        response = _http.get(url, headers={"User-Agent": user_agent})
    except httpx.HTTPError as exc:
        raise ConnectionError(f"gagal terhubung ke {urlparse(url).netloc}: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise ValueError(f"halaman membalas HTTP {response.status_code}")
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise ValueError(f"URL bukan halaman HTML: {content_type}")
    return parse_html(response.text, str(response.url))


def normalize_url(url: str) -> str:
    # "/id" dan "/id/" adalah halaman yang sama; fragmen "#..." tidak mengubah isi
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))
