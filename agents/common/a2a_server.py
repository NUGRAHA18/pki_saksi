"""Server A2A buatan sendiri (Fase 3, Bab 25): Agent Card, JSON-RPC, dan siklus hidup task.

Mengikuti spesifikasi A2A (protocolVersion 0.3) sebatas yang dipakai SAKSI:
- Agent Card di GET /.well-known/agent-card.json (juga /.well-known/agent.json, nama lama).
- JSON-RPC 2.0 di POST /: message/send, tasks/get, tasks/cancel. Streaming (message/stream, SSE) dan push
  notification tidak dibuat; Agent Card menyatakannya dengan capabilities.streaming = false.
- Status task: submitted -> working -> completed / failed / canceled, atau input-required (agent butuh masukan;
  klien melanjutkan dengan message/send ber-taskId sama: interaksi multi-turn). Task berstatus terminal tidak bisa
  dilanjutkan; pekerjaan lanjutan memakai task baru dalam contextId yang sama.
- Keamanan (Bab 25.8): token bearer opsional (SAKSI_A2A_TOKEN). Agent Card tetap publik supaya bisa ditemukan.
- Audit (Bab 25.8.4): setiap perubahan status dicatat dengan correlation_id dari message.metadata.

Agent cukup menulis satu handler:  async def handler(ctx: TaskContext) -> Completed | InputRequired | Failed
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .audit import AuditEvent, AuditLog

PROTOCOL_VERSION = "0.3.0"
TERMINAL = {"completed", "failed", "canceled", "rejected"}

# kode error JSON-RPC (standar) dan kode khusus A2A
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
TASK_NOT_FOUND, TASK_NOT_CANCELABLE, UNSUPPORTED_OPERATION = -32001, -32002, -32004


# --- bentuk pesan A2A ------------------------------------------------------------------------------------------------

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id() -> str:
    return uuid.uuid4().hex


def text_part(text: str) -> dict:
    return {"kind": "text", "text": text}


def data_part(data: dict) -> dict:
    return {"kind": "data", "data": data}


def make_message(role: str, parts: list[dict], **extra) -> dict:
    """role: "user" (pengirim task) atau "agent". extra: taskId, contextId, metadata, referenceTaskIds."""
    return {"kind": "message", "messageId": new_id(), "role": role, "parts": parts,
            **{key: value for key, value in extra.items() if value}}


def make_artifact(name: str, parts: list[dict], description: str = "") -> dict:
    artifact = {"artifactId": new_id(), "name": name, "parts": parts}
    if description:
        artifact["description"] = description
    return artifact


# --- hasil handler ---------------------------------------------------------------------------------------------------

@dataclass
class Completed:
    artifacts: list[dict]
    message: str = ""


@dataclass
class InputRequired:
    question: str  # dikirim ke klien sebagai status.message


@dataclass
class Failed:
    reason: str
    artifacts: list[dict] = field(default_factory=list)


@dataclass
class TaskContext:
    task_id: str
    context_id: str
    message: dict  # pesan terbaru dari klien
    history: list[dict]  # semua pesan task ini, termasuk balasan agent (untuk multi-turn)
    correlation_id: str

    def text(self) -> str:
        return "\n".join(part["text"] for part in self.message.get("parts", []) if part.get("kind") == "text")

    def data(self) -> dict:
        merged: dict = {}
        for part in self.message.get("parts", []):
            if part.get("kind") == "data":
                merged.update(part.get("data") or {})
        return merged


Handler = Callable[[TaskContext], Awaitable[Completed | InputRequired | Failed]]


class RPCError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- server ----------------------------------------------------------------------------------------------------------

def agent_card(name: str, description: str, url: str, skills: list[dict], secured: bool = False,
               version: str = "0.1.0") -> dict:
    card = {
        "protocolVersion": PROTOCOL_VERSION,
        "name": name,
        "description": description,
        "url": url,
        "preferredTransport": "JSONRPC",
        "version": version,
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": skills,
    }
    if secured:
        card["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer"}}
        card["security"] = [{"bearer": []}]
    return card


class A2AServer:
    def __init__(self, card: dict, handler: Handler, audit: AuditLog | None = None, token: str = "",
                 task_timeout: float = 600, lifespan=None) -> None:
        self.card = card
        self.handler = handler
        self.audit = audit
        self.token = token
        self.task_timeout = task_timeout
        self.tasks: dict[str, dict] = {}
        self._correlation: dict[str, str] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._done: dict[str, asyncio.Event] = {}
        self.app = FastAPI(title=card["name"], lifespan=lifespan)
        self.app.get("/.well-known/agent-card.json")(self._card)
        self.app.get("/.well-known/agent.json")(self._card)
        self.app.post("/")(self._rpc)

    async def _card(self) -> dict:
        return self.card

    # JSON-RPC ----------------------------------------------------------------------------------------------------
    async def _rpc(self, request: Request) -> JSONResponse:
        if self.token and request.headers.get("authorization") != f"Bearer {self.token}":
            return JSONResponse({"error": "token tidak valid"}, status_code=401)
        try:
            body = await request.json()
        except ValueError:
            return self._error(None, PARSE_ERROR, "JSON tidak valid")
        request_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
            return self._error(request_id, INVALID_REQUEST, "bukan permintaan JSON-RPC 2.0")
        methods = {"message/send": self._send, "tasks/get": self._get, "tasks/cancel": self._cancel}
        method = methods.get(body["method"])
        if method is None:
            code = UNSUPPORTED_OPERATION if body["method"] in {"message/stream", "tasks/resubscribe"} else METHOD_NOT_FOUND
            return self._error(request_id, code, f"metode tidak didukung: {body['method']}")
        try:
            result = await method(body.get("params") or {})
        except RPCError as exc:
            return self._error(request_id, exc.code, str(exc))
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

    @staticmethod
    def _error(request_id, code: int, message: str) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    # metode ------------------------------------------------------------------------------------------------------
    async def _send(self, params: dict) -> dict:
        message = params.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("parts"), list) or not message["parts"]:
            raise RPCError(INVALID_PARAMS, "params.message.parts wajib diisi")
        message = {**message, "role": "user"}
        correlation_id = (message.get("metadata") or {}).get("correlation_id", "")

        if message.get("taskId"):  # lanjutan multi-turn
            task = self._task(message["taskId"])
            state = task["status"]["state"]
            if state in TERMINAL:
                raise RPCError(UNSUPPORTED_OPERATION, f"task sudah {state}; kirim task baru dalam contextId yang sama")
            if state != "input-required":
                raise RPCError(UNSUPPORTED_OPERATION, f"task sedang {state}, belum meminta masukan")
            message["contextId"] = task["contextId"]
            task["history"].append(message)
            correlation_id = correlation_id or self._correlation.get(task["id"], "")
            # status diubah saat itu juga: kalau menunggu _run, klien yang langsung bertanya masih melihat
            # input-required dan mengira task belum dilanjutkan
            self._set_state(task, "working", correlation_id=correlation_id)
        else:
            task_id = new_id()
            context_id = message.get("contextId") or new_id()
            message.update(taskId=task_id, contextId=context_id)
            task = {"kind": "task", "id": task_id, "contextId": context_id, "status": {}, "artifacts": [],
                    "history": [message], "metadata": {"correlation_id": correlation_id}}
            self.tasks[task_id] = task
            self._set_state(task, "submitted", correlation_id=correlation_id)

        self._correlation[task["id"]] = correlation_id
        self._done[task["id"]] = asyncio.Event()
        self._running[task["id"]] = asyncio.create_task(self._run(task, message, correlation_id))
        if (params.get("configuration") or {}).get("blocking"):
            await self._done[task["id"]].wait()
        return self._view(task, (params.get("configuration") or {}).get("historyLength"))

    async def _get(self, params: dict) -> dict:
        return self._view(self._task(params.get("id", "")), params.get("historyLength"))

    async def _cancel(self, params: dict) -> dict:
        task = self._task(params.get("id", ""))
        if task["status"]["state"] in TERMINAL:
            raise RPCError(TASK_NOT_CANCELABLE, f"task sudah {task['status']['state']}")
        running = self._running.get(task["id"])
        if running and not running.done():
            running.cancel()
        self._set_state(task, "canceled", "dibatalkan oleh klien")
        self._done[task["id"]].set()
        return self._view(task)

    # eksekusi ----------------------------------------------------------------------------------------------------
    async def _run(self, task: dict, message: dict, correlation_id: str) -> None:
        if task["status"]["state"] != "working":
            self._set_state(task, "working", correlation_id=correlation_id)
        context = TaskContext(task["id"], task["contextId"], message, list(task["history"]), correlation_id)
        try:
            outcome = await asyncio.wait_for(self.handler(context), timeout=self.task_timeout)
        except asyncio.CancelledError:
            return  # status sudah diubah oleh _cancel
        except asyncio.TimeoutError:
            outcome = Failed(f"timeout: task melebihi {self.task_timeout:.0f} detik")
        except Exception as exc:  # handler crash tidak boleh menjatuhkan server
            outcome = Failed(f"agent error: {type(exc).__name__}: {exc}")
        if task["status"]["state"] == "canceled":
            return
        if isinstance(outcome, Completed):
            task["artifacts"] += outcome.artifacts
            self._set_state(task, "completed", outcome.message, correlation_id)
        elif isinstance(outcome, InputRequired):
            self._set_state(task, "input-required", outcome.question, correlation_id)
        else:
            task["artifacts"] += outcome.artifacts
            self._set_state(task, "failed", outcome.reason, correlation_id)
        self._done[task["id"]].set()

    def _set_state(self, task: dict, state: str, text: str = "", correlation_id: str = "") -> None:
        status = {"state": state, "timestamp": now()}
        if text:
            status["message"] = make_message("agent", [text_part(text)], taskId=task["id"], contextId=task["contextId"])
            task["history"].append(status["message"])
        task["status"] = status
        if self.audit:
            self.audit.log(AuditEvent(correlation_id or self._correlation.get(task["id"], ""), self.card["name"],
                                      self.card["name"], "status_task", task_id=task["id"], status=state, isi=text))

    def _task(self, task_id: str) -> dict:
        if task_id not in self.tasks:
            raise RPCError(TASK_NOT_FOUND, f"task tidak ditemukan: {task_id}")
        return self.tasks[task_id]

    @staticmethod
    def _view(task: dict, history_length: int | None = None) -> dict:
        history = list(task["history"])
        if history_length is not None:  # historyLength=N: hanya N pesan terakhir
            history = history[max(0, len(history) - int(history_length)):] if int(history_length) > 0 else []
        return {**task, "status": dict(task["status"]), "artifacts": list(task["artifacts"]), "history": history}


def serve(server: A2AServer, port: int, host: str = "127.0.0.1") -> None:
    import uvicorn

    uvicorn.run(server.app, host=host, port=port, log_level="warning")
