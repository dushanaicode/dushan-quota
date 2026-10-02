import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

import quota


class UpgradeInstallMethodTests(unittest.TestCase):
    def setUp(self):
        root = Path.cwd() / "Temp"
        root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source with spaces"
        self.source.mkdir()
        self.prefix = self.root / "custom-tools" / "dushan-quota"
        self.prefix.mkdir(parents=True)
        self.patches = ExitStack()
        self.patches.enter_context(patch.object(quota, "ROOT", self.source))
        self.patches.enter_context(patch.object(sys, "prefix", str(self.prefix)))
        self.dist = self.patches.enter_context(patch.object(metadata, "distribution")).return_value
        self.dist.read_text.return_value = None

    def tearDown(self):
        self.patches.close()
        assert self.root.resolve().is_relative_to((Path.cwd() / "Temp").resolve())
        self.temporary.cleanup()

    def test_editable_metadata_takes_priority_over_tool_manager(self):
        for marker in ("uv-receipt.toml", "pipx_metadata.json"):
            with self.subTest(marker=marker):
                (self.prefix / marker).touch()
                self.dist.read_text.return_value = json.dumps({"dir_info": {"editable": True}})
                self.assertEqual(quota._install_method(), "editable")
                self.assertEqual(quota._upgrade_command(), ["git", "-C", str(self.source), "pull", "--ff-only"])
                (self.prefix / marker).unlink()

    def test_tool_receipts_detect_custom_install_directories(self):
        for marker, method in (("uv-receipt.toml", "uv"), ("pipx_metadata.json", "pipx")):
            with self.subTest(method=method):
                (self.prefix / marker).touch()
                self.assertEqual(quota._install_method(), method)
                (self.prefix / marker).unlink()

    def test_plain_install_uses_current_python(self):
        for direct in (None, {"dir_info": {"editable": False}}, {"archive_info": {}}):
            with self.subTest(direct=direct):
                self.dist.read_text.return_value = json.dumps(direct) if direct is not None else None
                self.assertEqual(quota._install_method(), "pip")
                self.assertEqual(quota._upgrade_command(), [sys.executable, "-m", "pip", "install", "--upgrade", "dushan-quota"])

    def test_source_checkout_does_not_upgrade_unrelated_installed_distribution(self):
        (self.source / ".git").mkdir()
        (self.prefix / "pipx_metadata.json").touch()
        self.assertEqual(quota._install_method(), "source")
        self.assertEqual(quota._upgrade_command()[0], "git")

    def test_missing_distribution_is_source_mode(self):
        self.patches.enter_context(patch.object(metadata, "distribution", side_effect=metadata.PackageNotFoundError))
        self.assertEqual(quota._install_method(), "source")

    def test_package_manager_selection_is_used_by_the_upgrade_menu(self):
        commands = {
            "uv": ["uv", "tool", "upgrade", "dushan-quota"],
            "pipx": ["pipx", "upgrade", "--index-url", "https://pypi.org/simple", "--pip-args=pip==25.2", "dushan-quota"],
            "pip": [sys.executable, "-m", "pip", "install", "--upgrade", "dushan-quota"],
        }
        for method, command in commands.items():
            with self.subTest(method=method), patch.object(quota, "_install_method", return_value=method), patch.object(
                sys, "argv", ["quota.py"]
            ), patch.object(quota.config, "load_config", return_value={}), patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess(command, 0)
            ) as run:
                lines = []
                proceed = quota._startup_update(version="0.7.6", update_result={
                    "ok": True, "update_available": True, "latest_version": "0.7.7",
                }, input_fn=lambda _: "1", output=lines.append, interactive=True)
                self.assertFalse(proceed)
                run.assert_called_once_with(command, check=True, shell=False)
                self.assertIn("升级命令执行完成", "\n".join(lines))
                if method != "pipx":
                    self.assertNotIn("pipx", "\n".join(lines))

    def test_source_upgrade_requires_clean_tree_and_only_fast_forwards(self):
        for method in ("editable", "source"):
            with self.subTest(method=method), patch.object(quota, "_install_method", return_value=method), patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=b"")
            ) as run, patch.object(subprocess, "Popen") as popen, patch.object(sys, "argv", ["quota"]):
                lines = []
                quota._run_upgrade(output=lines.append)
                self.assertEqual(run.call_args_list[0].args[0], ["git", "-C", str(self.source), "status", "--porcelain"])
                self.assertEqual(run.call_args_list[1].args[0], ["git", "-C", str(self.source), "pull", "--ff-only"])
                self.assertEqual(run.call_count, 2)
                popen.assert_not_called()
                self.assertIn("升级命令执行完成", "\n".join(lines))

    def test_dirty_source_does_not_pull_or_modify_local_work(self):
        with patch.object(quota, "_install_method", return_value="editable"), patch.object(
            subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=b" M quota.py\n?? \xff.txt\n")
        ) as run:
            lines = []
            quota._run_upgrade(output=lines.append)
        self.assertEqual(run.call_count, 1)
        self.assertIn("源码有本地改动", "\n".join(lines))
        self.assertNotIn("升级命令执行完成", "\n".join(lines))

    def test_git_pull_failure_is_reported_without_retry(self):
        with patch.object(quota, "_install_method", return_value="editable"), patch.object(
            subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, stdout=b""),
                                            subprocess.CalledProcessError(128, ["git"])]
        ) as run:
            lines = []
            quota._run_upgrade(output=lines.append)
        self.assertEqual(run.call_count, 2)
        self.assertIn("退出码 128", "\n".join(lines))
        self.assertIn("git 输出", "\n".join(lines))
        self.assertNotIn("升级命令执行完成", "\n".join(lines))

    def test_missing_uv_reports_uv_instead_of_pipx(self):
        with patch.object(quota, "_install_method", return_value="uv"), patch.object(
            subprocess, "run", side_effect=FileNotFoundError("uv")
        ):
            lines = []
            quota._run_upgrade(output=lines.append)
        self.assertIn("未找到 uv", "\n".join(lines))
        self.assertNotIn("pipx", "\n".join(lines))

    def test_source_upgrade_instructions_identify_source_and_show_repository(self):
        with patch.object(quota, "_install_method", return_value="editable"):
            lines = []
            quota._print_upgrade_command(lines.append)
            quota._print_banner("0.7.7", lines.append, color=False)
        text = "\n".join(lines)
        self.assertIn(str(self.source), text)
        self.assertIn("--ff-only", text)
        self.assertIn("editable", text)
        self.assertNotIn("pipx", text)


if __name__ == "__main__":
    unittest.main()
