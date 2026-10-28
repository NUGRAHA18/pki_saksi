"""Lokasi folder proyek dan pembacaan API key dari .env (tanpa paket tambahan)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
LOG_DIR = ROOT / "logs"
CACHE_DIR = ROOT / "data" / "cache"


def env(name: str, default: str = "") -> str:
    """Nilai dari environment, kalau tidak ada dari file .env; dipakai server MCP untuk API key."""
    if os.environ.get(name):
        return os.environ[name]
    if ENV_FILE.exists():
        with ENV_FILE.open(encoding="utf-8-sig") as file:
            for line in file:
                key, _, value = line.strip().partition("=")
                if key.strip() == name and value.strip():
                    return value.strip().strip("'\"")
    return default


def user_agent() -> str:
    """User-Agent untuk API & halaman web. Wikipedia menolak (403) User-Agent tanpa kontak (URL atau email),
    jadi kontak diambil dari SAKSI_CONTACT di .env."""
    contact = env("SAKSI_CONTACT")
    return f"SAKSI/0.1 ({contact}; proyek kuliah PKI, pemeriksa hoaks)" if contact else "SAKSI/0.1 (proyek kuliah PKI)"
