"""Klien A2A (Fase 3, Bab 25): membaca Agent Card, mengirim task, memantau status, dan mengambil artifact.

Pola yang dipakai Orchestrator: kirim non-blocking (message/send langsung mengembalikan task berstatus submitted),
lalu tanyakan status lewat tasks/get sampai terminal atau input-required. Dengan begitu task yang lama (Worker bisa
butuh beberapa menit karena kuota LLM) tidak terputus oleh timeout HTTP, dan setiap perubahan status terlihat.

    async with A2AClient("http://127.0.0.1:8101") as worker:
        card = await worker.card()
        task = await worker.run([data_part({"klaim": "..."})], correlation_id=cid)
        hasil = artifact_data(task, "hasil_pemeriksaan")
"""
import asyncio
import time

import httpx

from .a2a_server import TERMINAL, make_message, new_id
from .audit import AuditEvent, AuditLog
from .config import env


class A2AError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


def artifact_data(task: dict, name: str) -> dict | None:
    """Isi data part pertama dari artifact bernama `name`, atau None."""
    for artifact in task.get("artifacts", []):
        if artifact.get("name") == name:
            for part in artifact.get("parts", []):
                if part.get("kind") == "data":
                    return part["data"]
    return None


def status_text(task: dict) -> str:
    message = task.get("status", {}).get("message") or {}
    return "\n".join(part.get("text", "") for part in message.get("parts", []) if part.get("kind") == "text")


class A2AClient:
    def __init__(self, base_url: str, sender: str = "client", token: str | None = None, audit: AuditLog | None = None,
                 timeout: float = 30, transport: httpx.AsyncBaseTransport | None = None) -> None:
        token = env("SAKSI_A2A_TOKEN") if token is None else token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.base_url = base_url.rstrip("/")
        self.sender = sender
        self.audit = audit
        self._http = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=timeout, transport=transport)
        self._card: dict | None = None

    async def __aenter__(self) -> "A2AClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    async def card(self) -> dict:
        if self._card is None:
            response = await self._http.get("/.well-known/agent-card.json")
            response.raise_for_status()
            self._card = response.json()
        return self._card

    async def _rpc(self, method: str, params: dict) -> dict:
        response = await self._http.post("/", json={"jsonrpc": "2.0", "id": new_id(), "method": method,
                                                    "params": params})
        if response.status_code == 401:
            raise A2AError(401, "ditolak server: token A2A salah atau tidak ada")
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise A2AError(body["error"]["code"], body["error"]["message"])
        return body["result"]

    async def send(self, parts: list[dict], task_id: str = "", context_id: str = "", correlation_id: str = "",
                   blocking: bool = False, reference_task_ids: list[str] | None = None) -> dict:
        message = make_message("user", parts, taskId=task_id, contextId=context_id,
                               metadata={"correlation_id": correlation_id} if correlation_id else None,
                               referenceTaskIds=reference_task_ids)
        task = await self._rpc("message/send", {"message": message, "configuration": {"blocking": blocking}})
        await self._log(correlation_id, "task_dikirim", task)
        return task

    async def get(self, task_id: str, history_length: int | None = None) -> dict:
        params = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        return await self._rpc("tasks/get", params)

    async def cancel(self, task_id: str) -> dict:
        return await self._rpc("tasks/cancel", {"id": task_id})

    async def wait(self, task_id: str, poll_interval: float = 1.0, timeout: float = 900,
                   correlation_id: str = "") -> dict:
        """Menunggu sampai task terminal atau input-required. Bila melewati timeout, task dibatalkan."""
        deadline = time.monotonic() + timeout
        while True:
            task = await self.get(task_id, history_length=0)
            if task["status"]["state"] in TERMINAL | {"input-required"}:
                await self._log(correlation_id, "task_selesai", task)
                return task
            if time.monotonic() > deadline:
                task = await self.cancel(task_id)
                await self._log(correlation_id, "timeout", task)
                return task
            await asyncio.sleep(poll_interval)

    async def run(self, parts: list[dict], correlation_id: str = "", **kwargs) -> dict:
        """Kirim task lalu tunggu hasilnya (pola standar Orchestrator)."""
        timeout = kwargs.pop("timeout", 900)
        poll_interval = kwargs.pop("poll_interval", 1.0)
        task = await self.send(parts, correlation_id=correlation_id, **kwargs)
        if task["status"]["state"] in TERMINAL | {"input-required"}:
            return task
        return await self.wait(task["id"], poll_interval=poll_interval, timeout=timeout, correlation_id=correlation_id)

    async def _log(self, correlation_id: str, jenis: str, task: dict) -> None:
        if self.audit:
            receiver = (await self.card())["name"]
            self.audit.log(AuditEvent(correlation_id, self.sender, receiver, jenis, task_id=task["id"],
                                      status=task["status"]["state"], isi=status_text(task)))
