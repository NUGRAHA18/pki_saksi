"""Klien LLM (Gemma lewat Gemini API) dengan retry, keluaran JSON terstruktur, dan hitungan token (Bab 18.7).

Disalin dari SIAGA (siaga/src/llm.py); yang berubah hanya lokasi .env dan nama variabel model.
"""
from dataclasses import dataclass, replace
import json
import os
import re
import threading
import time
from typing import Protocol

from .config import ENV_FILE

DEFAULT_MODEL = os.environ.get("SAKSI_MODEL", "gemma-4-26b-a4b-it")  # gratis; set SAKSI_MODEL untuk model lain
MAX_ATTEMPTS = 7
# Keluaran macet (terpotong di MAX_OUTPUT_TOKENS, JSON rusak, tanpa tool call) yang sudah terjadi berkali-kali hampir
# tidak pernah pulih: di trace Fase 9 terjadi 7 kali berturut-turut (273 detik, ±21 ribu token terbuang). Setelah
# MAX_STUCK kali, berhenti; agent pemanggil mencatatnya dan klaim menjadi TIDAK TERVERIFIKASI.
MAX_STUCK = 3
RATE_LIMIT_COOLDOWN = 60.0  # detik: key yang kena batas per menit diistirahatkan selama ini
BACKOFF_SECONDS = 5.0
REQUEST_TIMEOUT_MS = 120_000
RETRY_CODES = {429, 500, 502, 503, 504}
# Jawaban normal jauh di bawah ini (ekstraksi 5 klaim ±400 token, jawab Worker dengan 3 kutipan <800). Lebih dari
# itu berarti model macet mengulang teks. Dulu 2048: Gemma butuh ±80 detik untuk menghasilkan 2048 token sampah
# sebelum percobaan dianggap gagal (terlihat di trace Fase 9), jadi batas lebih kecil = kegagalan terdeteksi lebih cepat.
MAX_OUTPUT_TOKENS = 1024
# temperature 0 deterministik: model yang macet hanya lepas bila diberi keacakan, dinaikkan tiap kali macet lagi
RETRY_TEMPERATURES = (0.3, 0.6, 1.0)


@dataclass(frozen=True)
class LLMResult:
    text: str
    token: int = 0
    percobaan: int = 1  # >1 berarti ada retry (kuota per menit, server sibuk, model macet)
    tunggu_detik: float = 0.0  # total waktu tidur backoff sebelum berhasil

    def json(self) -> dict:
        return json.loads(self.text)


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict


@dataclass(frozen=True)
class ChatResult:
    call: ToolCall
    token: int = 0
    percobaan: int = 1
    tunggu_detik: float = 0.0


class LLM(Protocol):
    name: str

    def generate(
        self, prompt: str, system: str = "", temperature: float = 0.0, schema: dict | None = None
    ) -> LLMResult: ...


class ToolCallingLLM(LLM, Protocol):
    def chat(self, messages: list[dict], system: str, tools: list[dict]) -> ChatResult:
        """Satu giliran percakapan; model WAJIB memilih satu tool (function calling, Bab 18.3).

        messages: {"role": "user", "text"} / {"role": "model", "call": ToolCall} / {"role": "tool", "name", "result"}.
        tools: {"name", "description", "parameters" (JSON schema)}.
        """
        ...


class LLMError(RuntimeError):
    def __init__(self, message: str, token: int = 0) -> None:
        super().__init__(message)
        self.token = token  # token yang tetap terpakai oleh percobaan-percobaan yang gagal (biaya nyata)


class _NoToolCall(ValueError):
    """Model menjawab teks biasa padahal wajib memanggil tool: diperlakukan seperti keluaran macet."""


def _tokens(response) -> int:
    usage = response.usage_metadata
    return (usage.total_token_count or 0) if usage else 0


def _contents(messages: list[dict]):
    from google.genai import types

    contents = []
    for message in messages:
        if message["role"] == "user":
            part = types.Part.from_text(text=message["text"])
        elif message["role"] == "model":
            part = types.Part.from_function_call(name=message["call"].name, args=message["call"].args)
        else:
            part = types.Part.from_function_response(name=message["name"], response=message["result"])
        contents.append(types.Content(role="model" if message["role"] == "model" else "user", parts=[part]))
    return contents


def _keys_from_env_file(path=ENV_FILE) -> list[str]:
    """Membaca semua GEMINI_API_KEY* dari file .env (jangan dibagikan/di-commit), urut sesuai file."""
    if not os.path.exists(path):
        return []
    keys = []
    with open(path, encoding="utf-8-sig") as file:
        for line in file:
            name, _, value = line.strip().partition("=")
            if not name.strip().startswith(("GEMINI_API_KEY", "GOOGLE_API_KEY")):
                continue
            # satu baris boleh berisi beberapa key, dipisah koma/spasi/titik koma
            for key in re.split(r"[\s,;]+", value.strip().strip("'\"")):
                key = key.strip("'\"")
                if key and key not in keys:
                    keys.append(key)
    return keys


def _is_daily_quota(error: Exception) -> bool:
    return "PerDay" in str(error)


def _key_unusable(error) -> bool:
    """Masalah milik key/project ini (bukan permintaannya): kuota harian, key tidak valid, model tidak tersedia."""
    message = str(error)
    return (_is_daily_quota(error) or error.code in {401, 403}
            or (error.code == 400 and "API key not valid" in message)
            or (error.code == 404 and "no longer available" in message))


def _hit_token_limit(response) -> bool:
    candidates = getattr(response, "candidates", None) or []
    return bool(candidates) and str(getattr(candidates[0], "finish_reason", "")).endswith("MAX_TOKENS")


class GeminiLLM:
    """Bisa memakai beberapa API key (key tiap anggota tim). Permintaan digilir antar-key (round-robin), sehingga bila
    key berasal dari project Google yang berbeda, batas token per menit ikut berlipat (Fase 9: pemeriksaan lambat
    karena antre batas per menit). Key yang kena batas per menit diistirahatkan RATE_LIMIT_COOLDOWN detik dan
    permintaan langsung pindah ke key lain tanpa menunggu; key yang kuota hariannya habis/tidak valid dikeluarkan."""

    def __init__(self, model: str = DEFAULT_MODEL, api_keys: list[str] | None = None, sleep=time.sleep) -> None:
        from google import genai  # dimuat hanya saat dipakai, agar tes tidak butuh paket/API key

        env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        api_keys = api_keys or ([env_key] if env_key else _keys_from_env_file())
        if not api_keys:
            raise LLMError("GEMINI_API_KEY belum di-set: isi file saksi/.env dengan baris GEMINI_API_KEY=...")
        options = {"timeout": REQUEST_TIMEOUT_MS}
        self.clients = [genai.Client(api_key=key, http_options=options) for key in api_keys]
        self.name = model
        self.sleep = sleep
        self._lock = threading.Lock()  # dipakai bersamaan oleh beberapa Worker (thread)
        self._turn = 0
        self._dead: set[int] = set()
        self._cool_until: dict[int, float] = {}

    @property
    def client(self):
        return self.clients[min(set(range(len(self.clients))) - self._dead, default=0)]

    def _pick(self, avoid: int | None = None) -> int | None:
        """Key berikutnya secara bergilir: yang tidak sedang istirahat didahulukan. None bila semua key mati."""
        with self._lock:
            alive = [i for i in range(len(self.clients)) if i not in self._dead]
            if not alive:
                return None
            order = sorted(alive, key=lambda i: (i - self._turn) % len(self.clients))
            self._turn += 1
            now = time.monotonic()
            ready = [i for i in order if self._cool_until.get(i, 0) <= now and i != avoid]
            if ready:
                return ready[0]
            return min(order, key=lambda i: self._cool_until.get(i, 0))  # semua istirahat: yang paling cepat pulih

    def _rest(self, key: int) -> None:
        with self._lock:
            self._cool_until[key] = time.monotonic() + RATE_LIMIT_COOLDOWN

    def _kill(self, key: int) -> None:
        with self._lock:
            if key not in self._dead:
                self._dead.add(key)
                print(f"[LLM] key #{key + 1} tidak bisa dipakai (kuota harian/akses); "
                      f"{len(self.clients) - len(self._dead)} key tersisa")

    def _config(self, system: str, temperature: float, schema: dict | None):
        from google.genai import types

        config = {"temperature": temperature, "max_output_tokens": MAX_OUTPUT_TOKENS}
        if system:
            config["system_instruction"] = system
        if schema:
            config["response_mime_type"] = "application/json"
            config["response_json_schema"] = schema
        if self.name.startswith("gemini-2.5-flash"):
            # mode "thinking" dimatikan: tugasnya pendek, dan biaya/latensi jadi jauh lebih kecil
            config["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        return types.GenerateContentConfig(**config)

    def _tool_config(self, system: str, temperature: float, tools: list[dict]):
        from google.genai import types

        declarations = [types.FunctionDeclaration(name=tool["name"], description=tool["description"],
                                                  parameters_json_schema=tool["parameters"]) for tool in tools]
        return types.GenerateContentConfig(
            temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS, system_instruction=system,
            tools=[types.Tool(function_declarations=declarations)],
            # mode ANY: setiap giliran harus berupa aksi (tool call); jawaban akhir pun lewat tool `answer`
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
        )

    def generate(
        self, prompt: str, system: str = "", temperature: float = 0.0, schema: dict | None = None
    ) -> LLMResult:
        def parse(response) -> LLMResult:
            text = response.text or ""
            if schema:
                json.loads(text)  # JSON rusak/terpotong dianggap gagal dan diulang
            return LLMResult(text, _tokens(response))

        return self._request(prompt, lambda t: self._config(system, t, schema), temperature, parse)

    def chat(self, messages: list[dict], system: str, tools: list[dict]) -> ChatResult:
        def parse(response) -> ChatResult:
            calls = response.function_calls or []
            if not calls:
                raise _NoToolCall("model tidak memanggil tool")
            # ReAct: satu aksi per langkah; panggilan tambahan dalam giliran yang sama diabaikan
            return ChatResult(ToolCall(calls[0].name, dict(calls[0].args or {})), _tokens(response))

        return self._request(_contents(messages), lambda t: self._tool_config(system, t, tools), 0.0, parse)

    def _request(self, contents, make_config, temperature: float, parse):
        import httpx
        from google.genai import errors

        last_error: Exception | None = None
        waited = 0.0  # dicatat di hasil supaya antre/backoff terlihat di audit log (Fase 6)
        # token dari respons yang dibuang (terpotong/macet, JSON rusak, tanpa tool call) tetap ditagih API: dijumlahkan
        # supaya biaya di audit log dan metrik Fase 8 tidak terlihat lebih murah dari kenyataan
        wasted = 0
        degenerate = 0  # berapa kali model macet (keluaran terpotong, JSON rusak, tanpa tool call, 500/504)
        stuck = 0  # keluaran macet (bukan error server/kuota); lihat MAX_STUCK
        key = self._pick()
        for attempt in range(MAX_ATTEMPTS):
            spent = 0
            if key is None:
                raise LLMError(f"semua API key tidak bisa dipakai: {last_error}", wasted)
            try:
                attempt_temperature = temperature
                if degenerate:
                    attempt_temperature = max(temperature, RETRY_TEMPERATURES[min(degenerate, len(RETRY_TEMPERATURES)) - 1])
                response = self.clients[key].models.generate_content(
                    model=self.name, contents=contents, config=make_config(attempt_temperature)
                )
                spent = _tokens(response)
                if _hit_token_limit(response):
                    wasted += spent
                    degenerate += 1
                    stuck += 1
                    last_error = LLMError(f"keluaran terpotong di {MAX_OUTPUT_TOKENS} token (model mengulang teks)")
                    if stuck >= MAX_STUCK:
                        break
                    continue
                result = parse(response)
                return replace(result, token=result.token + wasted, percobaan=attempt + 1, tunggu_detik=round(waited, 1))
            except errors.APIError as error:
                last_error = error
                if _key_unusable(error):
                    self._kill(key)
                    key = self._pick()
                    if key is not None:
                        continue
                    raise LLMError(f"Gemini menolak permintaan ({error.code}): {error.message}", wasted) from error
                if error.code not in RETRY_CODES:
                    raise LLMError(f"Gemini menolak permintaan ({error.code}): {error.message}", wasted) from error
                # 429 = batas per menit key ini; 500/504 bisa milik project key ini (ditemukan: satu key selalu 500
                # sementara key lain normal). Keduanya: istirahatkan key dan langsung pindah ke key lain yang siap,
                # tanpa menunggu. 500/504 tetap menaikkan temperature (lihat bawah) karena bisa juga disebabkan prompt.
                if error.code in {429, 500, 504}:
                    self._rest(key)
                    other = self._pick(avoid=key)
                    if other is not None and other != key and self._cool_until.get(other, 0) <= time.monotonic():
                        key = other
                        degenerate += error.code in {500, 504}
                        last_error = error
                        continue
                    key = other if other is not None else key
                # Gemma: 500/504 bisa berulang untuk prompt yang sama di temperature 0 (model macet), bukan server sibuk
                degenerate += error.code in {500, 504}
                last_error = error
            except (json.JSONDecodeError, _NoToolCall) as error:
                # JSON rusak / tanpa tool call: mengulang dengan temperature 0 akan menghasilkan keluaran yang sama
                wasted += spent
                degenerate += 1
                stuck += 1
                last_error = error
                if stuck >= MAX_STUCK:
                    break
                continue
            except httpx.TransportError as error:
                # gangguan jaringan (koneksi putus/timeout): coba lagi
                last_error = error
            if attempt < MAX_ATTEMPTS - 1:
                self.sleep(BACKOFF_SECONDS * 2**attempt)  # 5, 10, ... 160 detik (limit per menit, server sibuk)
                waited += BACKOFF_SECONDS * 2**attempt
        if stuck >= MAX_STUCK:
            raise LLMError(f"model macet {stuck} kali berturut-turut, berhenti: {last_error}", wasted)
        raise LLMError(f"Gemini gagal setelah {MAX_ATTEMPTS} percobaan: {last_error}", wasted)


if __name__ == "__main__":
    # cek kuota: python -m agents.common.llm  (satu permintaan kecil, ±350 token)
    from google.genai import errors

    llm = GeminiLLM()
    try:
        response = llm.client.models.generate_content(model=llm.name, contents="Balas: OK")
        print(f"Kuota masih ada: {llm.name} menjawab ({response.usage_metadata.total_token_count} token).")
    except errors.APIError as error:
        if _is_daily_quota(error):
            print("Kuota HARIAN habis: tunggu reset (sekitar pukul 14.00 WIB) atau pakai key lain.")
        elif error.code == 429:
            print("Batas PER MENIT tercapai: tunggu ±1 menit lalu coba lagi.")
        else:
            print(f"Ditolak ({error.code}): {error.message}")
