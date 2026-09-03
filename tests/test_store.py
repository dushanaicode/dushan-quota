import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import store


class StoreDirTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "Temp"
        temp_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temp_root)
        self.home = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {"DUSHAN_QUOTA_HOME": "", "QUOTA_CLI_HOME": ""},
        )
        self.environment.start()
        self.home_patch = patch("pathlib.Path.home", return_value=self.home)
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def test_fresh_install_uses_dushan_quota_directory(self):
        self.assertEqual(self.home / ".dushan-quota", store.store_dir())

    def test_legacy_directory_is_used_until_new_store_is_initialized(self):
        legacy = self.home / ".quota-cli"
        legacy.mkdir()
        (legacy / "accounts.json").write_text("{}", encoding="utf-8")

        self.assertEqual(legacy, store.store_dir())

        current = self.home / ".dushan-quota"
        current.mkdir()
        (current / "accounts.json").write_text("{}", encoding="utf-8")
        self.assertEqual(current, store.store_dir())

    def test_new_environment_variable_takes_precedence(self):
        selected = self.home / "selected"
        with patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": str(selected)}):
            self.assertEqual(selected, store.store_dir())


if __name__ == "__main__":
    unittest.main()
