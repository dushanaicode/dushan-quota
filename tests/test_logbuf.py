import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class LogbufTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "Temp"
        temp_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temp_root)
        self.environment = patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": self.temporary.name})
        self.environment.start()
        from lib import logbuf

        logbuf._BUFFER.clear()
        self.logbuf = logbuf

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_entries_and_level_filter(self):
        self.logbuf.info("hello", provider="x")
        self.logbuf.error("bad")

        entries = self.logbuf.entries()

        self.assertEqual(2, len(entries))
        self.assertEqual("hello", entries[0]["msg"])
        self.assertEqual({"provider": "x"}, entries[0]["ctx"])
        self.assertEqual(1, len(self.logbuf.entries(level="ERROR")))

    def test_persists_jsonl(self):
        self.logbuf.info("persisted")

        from lib.store import store_dir

        content = (store_dir() / "quota.log").read_text(encoding="utf-8")
        self.assertIn("persisted", content)

    def test_ring_buffer_limit(self):
        for index in range(600):
            self.logbuf.debug(f"m{index}")

        entries = self.logbuf.entries(limit=500)

        self.assertEqual(500, len(entries))
        self.assertEqual("m100", entries[0]["msg"])

    def test_unknown_level_falls_back_to_info(self):
        self.logbuf.log("VERBOSE", "x")

        self.assertEqual("INFO", self.logbuf.entries()[0]["level"])


if __name__ == "__main__":
    unittest.main()
