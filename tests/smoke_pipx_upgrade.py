"""Exercise menu option 1 with a real pipx install, entirely under project Temp.

Run with a temporary environment containing pipx 1.8.0. The old installation
receives the candidate CLI so its own launcher performs the upgrade. Only the
available-update response is fixed; pipx, PyPI, input and child processes are real.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OLD_VERSION = "0.6.4"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    allowed = (ROOT / "Temp").resolve()
    allowed.mkdir(exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="pipx-upgrade-", dir=allowed)).resolve()
    temporary = scratch / "tmp"
    temporary.mkdir()
    os.environ.update({
        "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary),
        "PIP_CACHE_DIR": str(scratch / "pip-cache"),
        "PIPX_HOME": str(scratch / "home"),
        "PIPX_BIN_DIR": str(scratch / "bin"),
        "PIPX_MAN_DIR": str(scratch / "man"),
        "PIPX_SHARED_LIBS": str(scratch / "shared"),
        "PIPX_DEFAULT_PYTHON": sys.executable,
        "DUSHAN_QUOTA_HOME": str(scratch / "state"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PATH": str(scratch / "bin") + os.pathsep + sysconfig.get_path("scripts") + os.pathsep + os.environ["PATH"],
    })
    from pipx import paths

    for name in ("home", "venvs", "logs", "trash", "venv_cache", "bin_dir", "man_dir", "shared_libs"):
        assert getattr(paths.ctx, name).resolve().is_relative_to(scratch), name
    pipx = shutil.which("pipx")
    assert pipx and Path(pipx).resolve().is_relative_to(allowed), "Run pipx from a Temp environment"

    def run(name, command, *, input=None):
        result = subprocess.run(
            command, input=input, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", cwd=ROOT, timeout=300,
        )
        (scratch / f"{name}.log").write_text(result.stdout, encoding="utf-8")
        print(result.stdout, flush=True)
        result.check_returncode()
        return result.stdout.strip()

    with urllib.request.urlopen("https://pypi.org/pypi/dushan-quota/json", timeout=20) as response:
        latest = json.load(response)["info"]["version"]
    assert latest != OLD_VERSION, "A newer public release is required"
    run("install", [pipx, "install", "--index-url", "https://pypi.org/simple", f"dushan-quota=={OLD_VERSION}"])
    launcher = Path(paths.ctx.bin_dir) / ("quota.exe" if os.name == "nt" else "quota")
    assert launcher.resolve().is_relative_to(scratch)
    assert run("version-before", [str(launcher), "--version"]) == f"Dushan Quota {OLD_VERSION}"

    environment = paths.ctx.venvs / "dushan-quota"
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site = Path(run("site", [str(interpreter), "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]))
    assert site.resolve().is_relative_to(scratch)
    update = {"ok": True, "update_available": True, "latest_version": latest}
    candidate = (ROOT / "quota.py").read_text(encoding="utf-8")
    candidate += (
        "\nfrom functools import partial\n"
        f"_startup_update = partial(_startup_update, interactive=True, update_result={update!r})\n"
    )
    (site / "quota.py").write_text(candidate, encoding="utf-8")

    output = run("upgrade", [str(launcher)], input="1\n")
    assert "升级命令执行完成" in output, output
    assert run("version-after", [str(launcher), "--version"]) == f"Dushan Quota {latest}"
    metadata = json.loads((environment / "pipx_metadata.json").read_text(encoding="utf-8"))
    assert metadata["main_package"]["package_version"] == latest
    result = {"platform": platform.platform(), "from": OLD_VERSION, "to": latest, "menu_upgrade": "passed"}
    (scratch / "checks.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
