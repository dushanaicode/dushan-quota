import unittest
from unittest.mock import patch

import quota


class CliStartupTests(unittest.TestCase):
    def test_banner_and_current_release_status_are_shown(self):
        lines = []

        proceed = quota._startup_update(
            version="0.1.1",
            update_result={"ok": True, "update_available": False, "message": "已是最新版本 v0.1.1"},
            output=lines.append,
            interactive=False,
        )

        self.assertTrue(proceed)
        text = "\n".join(lines)
        self.assertIn("Dushan Quota", text)
        self.assertIn("版本    v0.1.1", text)
        self.assertIn(quota.GITHUB_URL, text)
        self.assertIn(quota.INSTALL_COMMAND, text)
        self.assertIn(quota.UPGRADE_COMMAND, text)
        self.assertIn("已是最新版本", text)

    def test_upgrade_choice_prints_command_and_stops_launch(self):
        lines = []

        proceed = quota._startup_update(
            version="0.1.1",
            update_result={"ok": True, "update_available": True, "latest_version": "0.2.0"},
            input_fn=lambda _prompt: "1",
            output=lines.append,
            interactive=True,
        )

        self.assertFalse(proceed)
        self.assertIn(quota.UPGRADE_COMMAND, "\n".join(lines))

    @patch.object(quota.config, "save_config")
    @patch.object(quota.config, "load_config", return_value={"ignored_update_version": ""})
    def test_permanent_skip_saves_only_the_latest_version(self, load_config, save_config):
        lines = []

        proceed = quota._startup_update(
            version="0.1.1",
            update_result={"ok": True, "update_available": True, "latest_version": "0.2.0"},
            input_fn=lambda _prompt: "3",
            output=lines.append,
            interactive=True,
        )

        self.assertTrue(proceed)
        save_config.assert_called_once_with({"ignored_update_version": "0.2.0"})
        load_config.assert_called_once_with()
        self.assertIn("未来更高版本仍会提醒", "\n".join(lines))

    @patch.object(quota.config, "save_config")
    @patch.object(quota.config, "load_config", return_value={"ignored_update_version": "0.2.0"})
    def test_ignored_release_does_not_prompt_again(self, _load_config, save_config):
        lines = []

        proceed = quota._startup_update(
            version="0.1.1",
            update_result={"ok": True, "update_available": True, "latest_version": "0.2.0"},
            input_fn=lambda _prompt: self.fail("ignored release should not prompt"),
            output=lines.append,
            interactive=True,
        )

        self.assertTrue(proceed)
        save_config.assert_not_called()
        self.assertIn("已永久跳过 v0.2.0", "\n".join(lines))

    def test_launch_summary_includes_web_url(self):
        lines = []

        quota._print_launch_summary(True, lines.append)

        self.assertIn("悬浮窗：已启动", lines[0])
        self.assertIn("http://127.0.0.1:18765/", lines[1])


if __name__ == "__main__":
    unittest.main()
