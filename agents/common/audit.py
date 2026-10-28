"""Audit log antar-agent (Bab 25.8.4, 26.2.6): satu baris JSON per kejadian, dikelompokkan per correlation_id.

Pengganti StepLogger SIAGA (CSV per langkah). Di SAKSI satu pesan pengguna melibatkan beberapa agent yang
berjalan paralel, jadi setiap baris mencatat siapa memanggil siapa, bukan hanya nomor langkah.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import uuid

from .config import LOG_DIR


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class AuditEvent:
    correlation_id: str
    pengirim: str  # mis. "orchestrator"
    penerima: str  # mis. "worker", "critic", "mcp-wiki"
    jenis: str  # task_dikirim / task_selesai / tool_call / llm_call / error
    task_id: str = ""
    isi: str = ""  # ringkasan input/output (dipotong agar log tidak membengkak)
    status: str = ""
    token: int = 0
    waktu_ms: float = 0.0
    percobaan: int = 0  # khusus llm_call: jumlah percobaan ke API LLM (>1 = ada retry)
    tunggu_detik: float = 0.0  # khusus llm_call: waktu tidur backoff
    waktu: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))


class AuditLog:
    MAX_ISI = 500

    def __init__(self, path: str | Path = LOG_DIR / "audit.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # worker paralel menulis ke file yang sama

    def log(self, event: AuditEvent) -> None:
        record = asdict(event)
        record["isi"] = record["isi"][: self.MAX_ISI]
        with self._lock, self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read(self, correlation_id: str | None = None) -> list[AuditEvent]:
        if not self.path.exists():
            return []
        events = [AuditEvent(**json.loads(line)) for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        # baris lama (sebelum ada field percobaan/tunggu_detik) tetap terbaca karena field baru punya nilai default
        return [event for event in events if correlation_id in (None, event.correlation_id)]
