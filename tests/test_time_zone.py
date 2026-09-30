import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lib import config, float_win, web
from lib.snapshot import Snapshot


class TimeZoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        temp_root = Path.cwd() / "Temp" / "subscription-time-20260929"
        temp_root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=temp_root)
        assert Path(temporary.name).resolve().is_relative_to(temp_root.resolve())
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)

    def request(self, method, payload=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            body = json.dumps(payload) if payload is not None else None
            connection.request(method, "/api/config", body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_default_and_supported_zones_persist_without_changing_other_settings(self):
        self.assertEqual("", config.load_config()["time_zone"])
        status, data = self.request("GET")
        self.assertEqual(200, status)
        self.assertEqual("", data["time_zone"])
        self.assertEqual([list(item) for item in config.TIME_ZONES], data["time_zones"])
        cfg = config.load_config()
        cfg["watch_seconds"] = 42
        cfg["float"] = {"theme": "light"}
        config.save_config(cfg)

        for zone, _ in reversed(config.TIME_ZONES):
            with self.subTest(zone=zone):
                status, data = self.request("POST", {"time_zone": zone})
                self.assertEqual(200, status)
                self.assertEqual({"ok": True, "time_zone": zone}, data)
                persisted = config.load_config()
                self.assertEqual(zone, persisted["time_zone"])
                self.assertEqual(42, persisted["watch_seconds"])
                self.assertEqual({"theme": "light"}, persisted["float"])

    def test_web_and_float_share_changes_and_quota_refresh_payloads(self):
        self.request("POST", {"time_zone": "Asia/Shanghai"})
        api = float_win.Api()
        self.assertEqual("Asia/Shanghai", api.settings()["time_zone"])
        self.assertEqual(config.TIME_ZONES, api.settings()["time_zones"])
        self.assertEqual({"ok": True, "time_zone": "UTC"}, api.set_time_zone("UTC"))
        status, data = self.request("GET")
        self.assertEqual(200, status)
        self.assertEqual("UTC", data["time_zone"])

        shared = Snapshot(results=[], fetched_at=time.time(), from_cache=False)
        with patch.object(web.snapshot, "get_snapshot", return_value=shared), patch.object(
            float_win, "get_snapshot", return_value=shared
        ), patch.object(web.store, "list_stored", return_value=[]), patch(
            "lib.usage.activation_statuses", return_value={}
        ):
            self.assertEqual("UTC", web._quota_payload()["time_zone"])
            self.assertEqual("UTC", float_win._fetch_payload()["time_zone"])
            api.set_time_zone("Asia/Shanghai")
            self.assertEqual("Asia/Shanghai", web._quota_payload()["time_zone"])
            self.assertEqual("Asia/Shanghai", float_win._fetch_payload()["time_zone"])

    def test_watch_only_update_preserves_and_reports_time_zone(self):
        float_win.Api().set_time_zone("Asia/Shanghai")
        status, data = self.request("POST", {"watch_seconds": 90})
        self.assertEqual(200, status)
        self.assertEqual({"ok": True, "time_zone": "Asia/Shanghai"}, data)
        self.assertEqual(90, config.load_config()["watch_seconds"])

    def test_invalid_zone_is_rejected_without_saving_any_changes(self):
        api = float_win.Api()
        api.set_time_zone("Asia/Shanghai")
        before = config.config_path().read_bytes()
        for value in ("Mars/Olympus", " Asia/Shanghai ", None, 8, [], {}):
            with self.subTest(value=value):
                cfg = config.load_config()
                cfg["time_zone"] = value
                with self.assertRaisesRegex(ValueError, "不支持的时区"):
                    config.save_config(cfg)
                with self.assertRaisesRegex(ValueError, "不支持的时区"):
                    api.set_time_zone(value)
                status, data = self.request("POST", {"time_zone": value, "watch_seconds": 99})
                self.assertEqual(400, status)
                self.assertEqual("不支持的时区", data["error"])
                self.assertEqual(before, config.config_path().read_bytes())

    def test_float_appearance_save_does_not_overwrite_shared_zone_or_input(self):
        api = float_win.Api()
        api.set_time_zone("UTC")
        old_settings = api.settings()
        old_settings["theme"] = "light"
        self.request("POST", {"time_zone": "Asia/Shanghai"})

        api.save_settings(old_settings)

        saved = config.load_config()
        self.assertEqual("Asia/Shanghai", saved["time_zone"])
        self.assertEqual("light", saved["float"]["theme"])
        self.assertNotIn("time_zone", saved["float"])
        self.assertNotIn("time_zones", saved["float"])
        self.assertEqual("UTC", old_settings["time_zone"])

    def test_float_settings_does_not_mutate_loaded_config(self):
        cfg = config.default_config()
        cfg["float"] = {"theme": "light"}
        with patch.object(config, "load_config", return_value=cfg):
            settings = float_win.Api().settings()
        self.assertEqual("", settings["time_zone"])
        self.assertEqual({"theme": "light"}, cfg["float"])


if __name__ == "__main__":
    unittest.main()
