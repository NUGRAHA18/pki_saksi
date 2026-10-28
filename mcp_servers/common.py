"""Bagian bersama keempat server MCP: HTTP, cache disk, daftar blokir domain, dan kuota harian.

- Cache: hasil API disimpan di data/cache/<server>/ supaya run ulang (eksperimen Fase 8) tidak menghabiskan kuota
  dan hasilnya bisa diulang persis (RENCANA §8).
- Daftar blokir: sumber label test set diblokir saat evaluasi agar sistem tidak sekadar mencocokkan artikel
  yang dipakai sebagai kunci jawaban (RENCANA §7, kontrol kebocoran data). Diisi lewat env SAKSI_BLOCKLIST
  (dipisah koma), file di env SAKSI_BLOCKLIST_FILE (dipakai eval/run_eval.py, hanya selama evaluasi), atau file
  data/blocklist.txt (permanen). Satu domain/URL per baris.
- Kuota: penghitung request per hari per API, karena paket gratis (GNews 100/hari) cepat habis.
"""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import threading
from urllib.parse import urlparse

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from agents.common.config import CACHE_DIR, ROOT, env, user_agent

BLOCKLIST_FILE = ROOT / "data" / "blocklist.txt"
QUOTA_FILE = CACHE_DIR / "kuota.json"

# httpx mencatat URL lengkap di level INFO, termasuk parameter key=/apikey=; jangan sampai API key masuk log
logging.getLogger("httpx").setLevel(logging.WARNING)

_client = httpx.Client(timeout=20, headers={"User-Agent": user_agent()}, follow_redirects=True)
_quota_lock = threading.Lock()


def get_json(url: str, params: dict, headers: dict | None = None) -> dict:
    """GET yang mengembalikan JSON; kegagalan diubah menjadi ToolError supaya pesannya sampai ke agent."""
    for attempt in (1, 2):  # gangguan koneksi sesaat (ditemukan: ConnectError ke Wikipedia) diulang sekali
        try:
            response = _client.get(url, params=params, headers=headers)
            break
        except httpx.TransportError as exc:
            if attempt == 2:
                raise ToolError(f"gagal menghubungi {urlparse(url).netloc}: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"gagal menghubungi {urlparse(url).netloc}: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise ToolError(f"{urlparse(url).netloc} membalas {response.status_code}: {_error_message(response)}")
    return response.json()


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error") or body.get("errors") or body
    if isinstance(error, dict):
        error = error.get("message", error)
    return str(error)[:200]


# --- cache ---------------------------------------------------------------------------------------------------------

def _cache_path(server: str, key: dict) -> Path:
    digest = hashlib.sha1(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return CACHE_DIR / server / f"{digest}.json"


def cache_get(server: str, key: dict, max_age: timedelta) -> dict | None:
    path = _cache_path(server, key)
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if datetime.now(timezone.utc) - datetime.fromisoformat(record["waktu"]) > max_age:
        return None
    return record["data"]


def cache_put(server: str, key: dict, data: dict) -> None:
    path = _cache_path(server, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"waktu": datetime.now(timezone.utc).isoformat(), "key": key, "data": data}
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


# --- daftar blokir -------------------------------------------------------------------------------------------------

def blocklist() -> list[str]:
    entries = [item for item in env("SAKSI_BLOCKLIST").split(",")]
    for path in (BLOCKLIST_FILE, Path(env("SAKSI_BLOCKLIST_FILE")) if env("SAKSI_BLOCKLIST_FILE") else None):
        if path is not None and path.exists():
            entries += path.read_text(encoding="utf-8").splitlines()
    return [entry.strip().lower() for entry in entries if entry.strip() and not entry.strip().startswith("#")]


def is_blocked(url: str, entries: list[str] | None = None) -> bool:
    """True bila host URL sama dengan / subdomain dari domain yang diblokir, atau URL diawali entri yang diblokir."""
    entries = blocklist() if entries is None else entries
    lowered = url.lower()
    host = urlparse(lowered).netloc.removeprefix("www.")
    for entry in entries:
        if "/" in entry:
            if lowered.split("://", 1)[-1].removeprefix("www.").startswith(entry.split("://", 1)[-1].removeprefix("www.")):
                return True
        elif host == entry.removeprefix("www.") or host.endswith("." + entry.removeprefix("www.")):
            return True
    return False


# --- kuota harian --------------------------------------------------------------------------------------------------

def use_quota(api: str, daily_limit: int) -> int:
    """Mencatat satu request ke API; ToolError bila batas harian sudah tercapai. Mengembalikan sisa kuota."""
    today = date.today().isoformat()
    with _quota_lock:
        data = json.loads(QUOTA_FILE.read_text(encoding="utf-8")) if QUOTA_FILE.exists() else {}
        used = data.get(today, {}).get(api, 0)
        if used >= daily_limit:
            raise ToolError(f"kuota harian {api} habis ({used}/{daily_limit}); coba sumber lain")
        data = {today: {**data.get(today, {}), api: used + 1}}  # hari sebelumnya tidak perlu disimpan
        QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
        QUOTA_FILE.write_text(json.dumps(data), encoding="utf-8")
    return daily_limit - used - 1


def quota_used(api: str) -> int:
    if not QUOTA_FILE.exists():
        return 0
    return json.loads(QUOTA_FILE.read_text(encoding="utf-8")).get(date.today().isoformat(), {}).get(api, 0)
