"""Verify uv editable menu upgrades against a local Git remote under project Temp."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main():
    allowed = (ROOT / "Temp").resolve()
    allowed.mkdir(exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="uv-upgrade-", dir=allowed)).resolve()
    assert scratch.is_relative_to(allowed)
    temporary = scratch / "tmp"
    temporary.mkdir()
    tools = scratch / "custom-tool-directory"
    binaries = scratch / "bin"
    os.environ.update({
        "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary),
        "UV_TOOL_DIR": str(tools), "UV_TOOL_BIN_DIR": str(binaries),
        "UV_CACHE_DIR": str(scratch / "uv-cache"),
        "UV_PYTHON_INSTALL_DIR": str(scratch / "python"), "UV_PYTHON_DOWNLOADS": "never",
        "PIP_CACHE_DIR": str(scratch / "pip-cache"),
        "DUSHAN_QUOTA_HOME": str(scratch / "state"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        "GIT_TERMINAL_PROMPT": "0", "UV_NO_PROGRESS": "1",
        "PATH": str(binaries) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
    })
    uv = Path(sys.executable).parent / ("uv.exe" if os.name == "nt" else "uv")
    assert uv.is_file() and uv.resolve().is_relative_to(allowed), "Install uv in a project Temp environment"

    def run(name, command, *, input=None):
        result = subprocess.run(command, input=input, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", cwd=ROOT, timeout=300)
        (scratch / f"{name}.log").write_text(result.stdout, encoding="utf-8")
        if result.returncode:
            print(result.stdout[-4000:], flush=True)
        result.check_returncode()
        return result.stdout.strip()

    upstream = scratch / "upstream"
    upstream.mkdir()
    for name in ("quota.py", "pyproject.toml", "README.md", "LICENSE", ".gitignore"):
        shutil.copy2(ROOT / name, upstream / name)
    shutil.copytree(ROOT / "lib", upstream / "lib", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    project = upstream / "pyproject.toml"
    project.write_text(re.sub(r'(?m)^version = ".*"$', 'version = "0.0.1"',
                              project.read_text(encoding="utf-8")), encoding="utf-8")
    candidate = (upstream / "quota.py").read_text(encoding="utf-8")
    guard = '\nif __name__ == "__main__":\n'
    assert candidate.count(guard) == 1
    hook = (
        "\nfrom functools import partial\n"
        "_startup_update = partial(_startup_update, interactive=True, "
        "update_result={'ok': True, 'update_available': True, 'latest_version': '0.0.2'})\n"
    )
    (upstream / "quota.py").write_text(candidate.replace(guard, hook + guard), encoding="utf-8")
    git = ["git", "-c", "user.name=Quota upgrade test", "-c", "user.email=quota-upgrade@example.test", "-C", str(upstream)]
    remote = scratch / "remote.git"
    source = scratch / "source with spaces"
    run("init-remote", ["git", "init", "--bare", "-b", "main", str(remote)])
    run("init-source", [*git, "init", "-b", "main"])
    run("stage-source", [*git, "add", "."])
    run("commit-before", [*git, "commit", "-m", "fixture before upgrade"])
    run("add-remote", [*git, "remote", "add", "origin", str(remote)])
    run("push-before", [*git, "push", "-u", "origin", "main"])
    run("clone", ["git", "clone", str(remote), str(source)])
    run("install", [str(uv), "tool", "install", "--python", sys.executable, "--editable", str(source)])
    environment = tools / "dushan-quota"
    assert (environment / "uv-receipt.toml").is_file()
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    launcher = binaries / ("quota.exe" if os.name == "nt" else "quota")
    assert launcher.resolve().is_relative_to(scratch)
    detected = run("detect", [str(interpreter), "-B", "-c", "import quota; print(quota._install_method())"])
    assert detected == "editable", detected
    assert run("version-before", [str(launcher), "--version"]) == "Dushan Quota 0.0.1"

    project.write_text(project.read_text(encoding="utf-8").replace('version = "0.0.1"', 'version = "0.0.2"'), encoding="utf-8")
    run("stage-upgrade", [*git, "add", "pyproject.toml"])
    run("commit-upgrade", [*git, "commit", "-m", "fixture after upgrade"])
    run("push-upgrade", [*git, "push"])
    output = run("upgrade", [str(launcher)], input="1\n")
    assert "升级命令执行完成" in output and "pipx" not in output, output
    assert run("version-after", [str(launcher), "--version"]) == "Dushan Quota 0.0.2"
    head = run("head", ["git", "-C", str(source), "rev-parse", "HEAD"])
    assert head == run("remote-head", [*git, "rev-parse", "HEAD"])
    local = source / "README.md"
    local.write_text(local.read_text(encoding="utf-8") + "\nlocal change to preserve\n", encoding="utf-8")
    before = local.read_bytes()
    dirty = run("dirty-upgrade", [str(launcher)], input="1\n")
    assert "源码有本地改动" in dirty and "升级命令执行完成" not in dirty, dirty
    assert local.read_bytes() == before
    assert head == run("head-after-dirty", ["git", "-C", str(source), "rev-parse", "HEAD"])
    result = {"platform": sys.platform, "method": detected, "from": "0.0.1", "to": "0.0.2",
              "menu_upgrade": "passed", "custom_tool_directory": True, "dirty_source_preserved": True}
    (scratch / "checks.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({**result, "evidence": str(scratch)}), flush=True)


if __name__ == "__main__":
    main()
