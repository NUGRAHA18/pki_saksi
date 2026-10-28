"""Tes Fase 6: klien LLM mencatat jumlah percobaan dan waktu tunggu backoff (tanpa jaringan)."""
from types import SimpleNamespace
import unittest

from google.genai import errors

from agents.common.llm import BACKOFF_SECONDS, GeminiLLM, LLMError


def response(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, usage_metadata=SimpleNamespace(total_token_count=42), candidates=[],
                           function_calls=None)


class FakeModels:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)

    def generate_content(self, **kwargs):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def rate_limited() -> errors.APIError:
    return errors.APIError(429, {"error": {"code": 429, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}})


class TestRetry(unittest.TestCase):
    def make(self, outcomes, *more_keys) -> tuple[GeminiLLM, list]:
        slept: list[float] = []
        llm = GeminiLLM(api_keys=["kunci-palsu"] * (1 + len(more_keys)), sleep=slept.append)
        llm.clients = [SimpleNamespace(models=FakeModels(o)) for o in (outcomes, *more_keys)]
        return llm, slept

    def test_percobaan_dan_waktu_tunggu_tercatat(self):
        llm, slept = self.make([rate_limited(), rate_limited(), response('{"ok": true}')])
        result = llm.generate("halo", schema={"type": "object"})
        self.assertEqual((result.percobaan, result.token), (3, 42))
        self.assertEqual(result.tunggu_detik, BACKOFF_SECONDS + BACKOFF_SECONDS * 2)
        self.assertEqual(slept, [BACKOFF_SECONDS, BACKOFF_SECONDS * 2])

    def test_token_percobaan_gagal_ikut_dihitung(self):
        truncated = SimpleNamespace(text="ulang ulang", usage_metadata=SimpleNamespace(total_token_count=2000),
                                    candidates=[SimpleNamespace(finish_reason="FinishReason.MAX_TOKENS")],
                                    function_calls=None)
        llm, _ = self.make([truncated, response('{"ok": true}')])
        self.assertEqual(llm.generate("halo", schema={"type": "object"}).token, 2042)
        llm, slept = self.make([truncated] * 7)
        with self.assertRaisesRegex(LLMError, "macet 3 kali") as caught:
            llm.generate("halo")
        self.assertEqual(caught.exception.token, 6000)  # berhenti setelah 3 kali macet, bukan 7
        self.assertEqual(slept, [])

    def test_kena_batas_per_menit_pindah_key_tanpa_menunggu(self):
        llm, slept = self.make([rate_limited()], [response('{"ok": true}')])
        result = llm.generate("halo", schema={"type": "object"})
        self.assertEqual((result.percobaan, result.tunggu_detik, slept), (2, 0.0, []))

    def test_error_500_pindah_key_tanpa_menunggu(self):
        internal = errors.APIError(500, {"error": {"code": 500, "message": "Internal error encountered.",
                                                   "status": "INTERNAL"}})
        llm, slept = self.make([internal], [response('{"ok": true}')])
        result = llm.generate("halo", schema={"type": "object"})
        self.assertEqual((result.percobaan, slept), (2, []))

    def test_permintaan_digilir_antar_key(self):
        first, second = [response("A"), response("C")], [response("B")]
        llm, _ = self.make(first, second)
        self.assertEqual([llm.generate("x").text for _ in range(3)], ["A", "B", "C"])

    def test_key_kuota_harian_habis_dikeluarkan(self):
        daily = errors.APIError(429, {"error": {"code": 429, "message": "Quota GenerateRequestsPerDay exceeded",
                                                "status": "RESOURCE_EXHAUSTED"}})
        llm, slept = self.make([daily], [response("B1"), response("B2")])
        self.assertEqual(llm.generate("x").text, "B1")
        self.assertEqual(llm.generate("x").text, "B2")  # key pertama tidak dipakai lagi
        self.assertEqual(slept, [])

    def test_berhasil_langsung(self):
        llm, slept = self.make([response("OK")])
        result = llm.generate("halo")
        self.assertEqual((result.percobaan, result.tunggu_detik, slept), (1, 0.0, []))

    def test_kuota_harian_tidak_diulang(self):
        daily = errors.APIError(429, {"error": {"code": 429, "message": "Quota GenerateRequestsPerDay exceeded",
                                                "status": "RESOURCE_EXHAUSTED"}})
        llm, slept = self.make([daily])
        with self.assertRaisesRegex(LLMError, "429"):
            llm.generate("halo")
        self.assertEqual(slept, [])


if __name__ == "__main__":
    unittest.main()
