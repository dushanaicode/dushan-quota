import io
import os
import re
import sys
import unittest
from contextlib import redirect_stdout
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
        self.assertIn("DUSHAN QUOTA", text)
        self.assertIn("v0.1.1", text)
        self.assertIn(quota.GITHUB_URL.removeprefix("https://"), text)
        self.assertIn(quota.PYPI_URL.removeprefix("https://"), text)
        self.assertIn(quota.WEB_URL, text)
        self.assertIn(f"pipx {quota.PIPX_VERSION} · pip {quota.PIP_VERSION}", text)
        self.assertIn("已是最新版本", text)

    def test_banner_supports_color_without_changing_layout_text(self):
        lines = []

        quota._print_banner("0.1.2", lines.append, color=True, width=68)

        text = "\n".join(lines)
        self.assertIn("\033[", text)
        self.assertIn("╭", text)
        self.assertIn("DUSHAN QUOTA", text)

    def test_default_and_float_launches_both_show_colored_startup_information(self):
        for args in ([], ["float"]):
            with self.subTest(args=args), patch.object(sys, "argv", ["quota", *args]), patch.object(
                quota, "_current_version", return_value="0.2.0"
            ), patch.object(quota, "_color_enabled", return_value=True), patch.object(
                quota.config, "apply_config_env"
            ), patch("lib.web._update_payload", return_value={
                "ok": True, "latest_version": "0.2.0", "update_available": False,
            }), patch("lib.float_win.launch_float", return_value=False) as launch, patch.dict(
                os.environ, {"DUSHAN_QUOTA_WEB_PORT": "18766", "DUSHAN_QUOTA_WINDOW_TITLE": "Quota-T"}
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    quota.main()
                text = output.getvalue()
                self.assertIn("\033[", text)
                self.assertIn("DUSHAN QUOTA", text)
                self.assertIn("当前 v0.2.0", text)
                self.assertIn("最新版本", text)
                self.assertIn("http://127.0.0.1:18766/", text)
                self.assertIn(quota.UPGRADE_COMMAND, text)
                self.assertIn("悬浮窗已在运行", text)
                launch.assert_called_once()

    def test_failed_release_check_keeps_upgrade_command_and_allows_launch(self):
        lines = []
        proceed = quota._startup_update(version="0.2.0", update_result={"ok": False, "error": "网络不可用"},
                                        output=lines.append, interactive=False)
        self.assertTrue(proceed)
        self.assertIn("查询失败", "\n".join(lines))
        self.assertIn(quota.UPGRADE_COMMAND, "\n".join(lines))

    def test_current_and_latest_versions_are_shown_separately(self):
        lines = []
        quota._startup_update(version="0.3.0", update_result={
            "ok": True, "latest_version": "0.2.0", "update_available": False,
        }, output=lines.append, interactive=False)
        text = "\n".join(lines)
        self.assertIn("当前 v0.3.0", text)
        self.assertIn("v0.2.0", text)
        self.assertIn(quota.UPGRADE_COMMAND, text)

    def test_narrow_banner_keeps_borders_aligned_and_does_not_truncate_links(self):
        lines = []
        quota._print_banner("0.2.0", lines.append, color=True, width=48)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(lines))
        for line in plain.splitlines():
            self.assertEqual(48, quota._cell_width(line))
        address = quota.GITHUB_URL + "/releases"
        wrapped = quota._panel_row("GitHub", address, 48, False).splitlines()
        recovered = "".join(line[12:-3].rstrip() for line in wrapped)
        self.assertEqual(address, recovered)

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

        self.assertIn("悬浮窗已启动", lines[0])
        self.assertIn("http://127.0.0.1:18765/", lines[1])


if __name__ == "__main__":
    unittest.main()
