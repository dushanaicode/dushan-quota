import unittest
import os
from unittest.mock import patch

from lib import web


class WebLaunchTests(unittest.TestCase):
    def test_configured_port_accepts_valid_value_and_rejects_invalid_values(self):
        with patch.dict(os.environ, {"DUSHAN_QUOTA_WEB_PORT": "18766"}):
            self.assertEqual(18766, web.configured_port())
        for value in ("bad", "0", "65536"):
            with self.subTest(value=value), patch.dict(os.environ, {"DUSHAN_QUOTA_WEB_PORT": value}):
                self.assertEqual(web.DEFAULT_WEB_PORT, web.configured_port())

    @patch.object(web.webbrowser, "open")
    @patch.object(web, "_wait_for_web", return_value=True)
    @patch("lib.float_win.launch_float")
    @patch.object(web, "_web_ready", return_value=False)
    def test_launches_float_window_when_no_server(self, ready, launch_float, wait, open_browser):
        result = web.launch_web(port=19001)

        self.assertTrue(result["ok"])
        self.assertTrue(result["started"])
        launch_float.assert_called_once_with()
        wait.assert_called_once_with("127.0.0.1", 19001, timeout=10.0)
        open_browser.assert_called_once_with("http://127.0.0.1:19001/")

    @patch.object(web.webbrowser, "open")
    @patch("lib.float_win.launch_float")
    @patch.object(web, "_web_ready", return_value=True)
    def test_reuses_running_server(self, ready, launch_float, open_browser):
        result = web.launch_web()

        self.assertTrue(result["ok"])
        self.assertFalse(result["started"])
        launch_float.assert_not_called()
        open_browser.assert_called_once()

    @patch.object(web.webbrowser, "open")
    @patch.object(web, "_wait_for_web", return_value=False)
    @patch("lib.float_win.launch_float")
    @patch.object(web, "_web_ready", return_value=False)
    def test_start_timeout_does_not_open_browser(self, ready, launch_float, wait, open_browser):
        result = web.launch_web()

        self.assertFalse(result["ok"])
        self.assertIn("超时", result["error"])
        open_browser.assert_not_called()

    @patch.object(web.webbrowser, "open")
    @patch.object(web, "_wait_for_web")
    @patch("lib.float_win.launch_float", side_effect=OSError("cannot spawn"))
    @patch.object(web, "_web_ready", return_value=False)
    def test_float_launch_failure_returns_without_waiting(self, ready, launch_float, wait, open_browser):
        result = web.launch_web()

        self.assertFalse(result["ok"])
        self.assertIn("悬浮窗启动失败", result["error"])
        wait.assert_not_called()
        open_browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
