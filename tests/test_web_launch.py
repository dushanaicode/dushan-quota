import unittest
from unittest.mock import Mock, patch

from lib import shell, web


class WebLaunchTests(unittest.TestCase):
    @patch.object(web.webbrowser, "open")
    @patch.object(web, "_wait_for_web", return_value=True)
    @patch.object(web, "_spawn_web_process")
    @patch.object(web, "_web_ready", return_value=False)
    def test_launches_server_out_of_process_and_returns(self, ready, spawn, wait, open_browser):
        result = web.launch_web(port=19001)

        self.assertTrue(result["ok"])
        self.assertTrue(result["started"])
        spawn.assert_called_once_with("127.0.0.1", 19001)
        wait.assert_called_once_with("127.0.0.1", 19001)
        open_browser.assert_called_once_with("http://127.0.0.1:19001/")

    @patch.object(web.webbrowser, "open")
    @patch.object(web, "_spawn_web_process")
    @patch.object(web, "_web_ready", return_value=True)
    def test_reuses_running_server(self, ready, spawn, open_browser):
        result = web.launch_web()

        self.assertTrue(result["ok"])
        self.assertFalse(result["started"])
        spawn.assert_not_called()
        open_browser.assert_called_once()

    @patch.object(web.webbrowser, "open")
    @patch.object(web, "_wait_for_web", return_value=False)
    @patch.object(web, "_spawn_web_process")
    @patch.object(web, "_web_ready", return_value=False)
    def test_start_timeout_does_not_open_browser(self, ready, spawn, wait, open_browser):
        result = web.launch_web()

        self.assertFalse(result["ok"])
        self.assertIn("超时", result["error"])
        open_browser.assert_not_called()

    @patch.object(web.webbrowser, "open")
    @patch.object(web, "_wait_for_web")
    @patch.object(web, "_spawn_web_process", side_effect=OSError("cannot spawn"))
    @patch.object(web, "_web_ready", return_value=False)
    def test_spawn_failure_returns_without_waiting(self, ready, spawn, wait, open_browser):
        result = web.launch_web()

        self.assertFalse(result["ok"])
        self.assertIn("启动失败", result["error"])
        wait.assert_not_called()
        open_browser.assert_not_called()

    @patch("builtins.print")
    @patch("builtins.input", side_effect=["6", "0"])
    @patch.object(
        web,
        "launch_web",
        return_value={"ok": True, "started": True, "url": "http://127.0.0.1:18765/"},
    )
    def test_interactive_menu_returns_after_launch(self, launch, user_input, output):
        show = Mock()

        shell.run_shell(show)

        launch.assert_called_once_with()
        show.assert_not_called()


if __name__ == "__main__":
    unittest.main()
