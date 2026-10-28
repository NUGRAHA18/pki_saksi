import tempfile
import unittest
from pathlib import Path

from agents.common.audit import AuditEvent, AuditLog, new_correlation_id
from agents.common.config import ROOT
from agents.common.llm import _keys_from_env_file
from mcp_servers.web import normalize_url, parse_html


class TestConfig(unittest.TestCase):
    def test_root_adalah_folder_proyek(self):
        self.assertTrue((ROOT / "requirements.txt").exists())

    def test_key_dibaca_dari_file_env(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("GEMINI_API_KEY=a, b\nGEMINI_API_KEY_2='c'\nFACTCHECK_API_KEY=x\n", encoding="utf-8")
            self.assertEqual(_keys_from_env_file(path), ["a", "b", "c"])


class TestAudit(unittest.TestCase):
    def test_event_disaring_per_correlation_id(self):
        with tempfile.TemporaryDirectory() as folder:
            log = AuditLog(Path(folder) / "audit.jsonl")
            first, second = new_correlation_id(), new_correlation_id()
            log.log(AuditEvent(first, "orchestrator", "worker", "task_dikirim", isi="x" * 2000))
            log.log(AuditEvent(second, "orchestrator", "critic", "task_dikirim"))
            events = log.read(first)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].penerima, "worker")
            self.assertEqual(len(events[0].isi), AuditLog.MAX_ISI)
            self.assertEqual(len(log.read()), 2)


class TestWeb(unittest.TestCase):
    def test_parse_html_membuang_script_dan_mengambil_judul(self):
        html = "<html><title>Cek Fakta</title><script>var x=1</script><p>Isi  berita</p><a href='/a'>x</a></html>"
        title, text, links = parse_html(html, "https://contoh.id/berita/")
        self.assertEqual(title, "Cek Fakta")
        self.assertEqual(text, "Isi berita x")
        self.assertEqual(links, ["https://contoh.id/a"])

    def test_normalize_url(self):
        self.assertEqual(normalize_url("HTTPS://Contoh.id/a/#bagian"), "https://contoh.id/a")


if __name__ == "__main__":
    unittest.main()
