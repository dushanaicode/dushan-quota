"""Exercise menu option 1 with a real pipx install, entirely under project Temp.

Run with a temporary environment containing pipx 1.8.0. The old installation
receives the candidate CLI with a fixed available-update response. On Windows,
test hooks hide the new console, record its handoff and answer the final close
prompt. Pipx, PyPI, the installed launcher and child processes remain real.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OLD_VERSION = "0.6.4"


def _wait_for_windows_process(pid):
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        assert ctypes.get_last_error() == 87, f"Cannot inspect worker {pid}"  # already exited
        return
    try:
        assert kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 10_000) == 0, "Worker did not exit"
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


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
    hook = (
        "\nfrom functools import partial\n"
        f"_startup_update = partial(_startup_update, interactive=True, update_result={update!r})\n"
    )
    if os.name == "nt":
        hook += f'''
import builtins
import ctypes
import json
from lib.snapshot import _process_exists
_scratch = Path({str(scratch)!r})
_real_popen = subprocess.Popen
_real_run = subprocess.run

def _console_pids():
    pids = (ctypes.c_ulong * 64)()
    count = ctypes.windll.kernel32.GetConsoleProcessList(pids, len(pids))
    assert count <= len(pids), "Console process buffer is too small"
    return list(pids[:count])

if sys.argv[1:2] == ["upgrade-run"]:
    _probe = {{"pid": os.getpid(), "messages": []}}
    _wait_pid = int(sys.argv[sys.argv.index("--wait-pid") + 1])

    def _record_output(line):
        print(line, flush=True)
        _probe["messages"].append(line)

    def _run_pipx(command, *args, **kwargs):
        _probe["launcher_exited_before_pipx"] = not _process_exists(_wait_pid)
        _probe["console_pids"] = _console_pids()
        _probe["stdio_isatty"] = [sys.stdin.isatty(), sys.stdout.isatty(), sys.stderr.isatty()]
        return _real_run(command, *args, **kwargs)

    def _close_window(prompt):
        print(prompt, flush=True)
        _probe["close_prompt"] = prompt
        pending = _scratch / "worker.pending"
        pending.write_text(json.dumps(_probe), encoding="utf-8")
        pending.replace(_scratch / "worker.json")
        return ""

    _run_upgrade = partial(_run_upgrade, output=_record_output)
    subprocess.run = _run_pipx
    builtins.input = _close_window
else:
    def _open_upgrade_window(command, *args, **kwargs):
        assert kwargs["creationflags"] == subprocess.CREATE_NEW_CONSOLE
        assert not {{"stdin", "stdout", "stderr"}}.intersection(kwargs), "Old terminal handles inherited"
        parent = {{"pid": os.getpid(), "console_pids": _console_pids()}}
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        process = _real_popen(command, *args, startupinfo=startup, **kwargs)
        parent["worker_pid"] = process.pid
        (_scratch / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
        return process

    subprocess.Popen = _open_upgrade_window
'''
    guard = '\nif __name__ == "__main__":\n'
    assert candidate.count(guard) == 1, "Candidate CLI main guard changed"
    candidate = candidate.replace(guard, hook + guard)
    (site / "quota.py").write_text(candidate, encoding="utf-8")

    output = run("upgrade", [str(launcher)], input="1\n")
    if os.name == "nt":
        deadline = time.monotonic() + 300
        while not (scratch / "worker.json").exists():
            assert time.monotonic() < deadline, f"Upgrade worker did not finish; evidence: {scratch}"
            time.sleep(0.1)
        worker = json.loads((scratch / "worker.json").read_text(encoding="utf-8"))
        parent = json.loads((scratch / "parent.json").read_text(encoding="utf-8"))
        # Windows venv python.exe may redirect to a second interpreter process.
        assert parent["worker_pid"] in worker["console_pids"]
        assert worker["pid"] in worker["console_pids"]
        assert parent["pid"] not in worker["console_pids"]
        assert not set(worker["console_pids"]).intersection(parent["console_pids"])
        assert worker["stdio_isatty"] == [True, True, True]
        assert worker["launcher_exited_before_pipx"]
        assert "按 Enter 关闭此窗口" in worker["close_prompt"]
        assert "升级命令执行完成" not in output, "Worker wrote into the old terminal"
        for pid in {worker["pid"], parent["worker_pid"]}:
            _wait_for_windows_process(pid)
        output = "\n".join(worker["messages"])
        print(output, flush=True)
    assert "升级命令执行完成" in output, output
    assert run("version-after", [str(launcher), "--version"]) == f"Dushan Quota {latest}"
    metadata = json.loads((environment / "pipx_metadata.json").read_text(encoding="utf-8"))
    assert metadata["main_package"]["package_version"] == latest
    result = {"platform": platform.platform(), "from": OLD_VERSION, "to": latest, "menu_upgrade": "passed"}
    if os.name == "nt":
        result.update(console_isolated=True, worker_exited=True)
    (scratch / "checks.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
