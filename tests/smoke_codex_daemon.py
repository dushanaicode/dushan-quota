"""Exercise real macOS Codex daemon restarts using synthetic auth in project Temp."""

import base64
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import provision
from lib.models import Account


def main():
    assert sys.platform == "darwin", "This smoke test requires macOS"
    from websockets.sync.client import unix_connect

    scratch = ROOT / "Temp/cdx8"
    scratch.mkdir()
    assert scratch.resolve().is_relative_to((ROOT / "Temp").resolve())
    state = scratch / "h"
    temporary = scratch / "t"
    state.mkdir()
    temporary.mkdir()
    # Leave enough space for macOS's Unix domain socket path limit.
    assert len(os.fsencode(state / "app-server-control/app-server-control.sock")) < 104
    codex_bin = ROOT / "Temp/codex-cli/node_modules/.bin/codex"
    assert codex_bin.is_file() and codex_bin.resolve().is_relative_to((ROOT / "Temp").resolve())
    os.environ.update({
        "CODEX_HOME": str(state), "CODEX_SQLITE_HOME": str(state),
        "DUSHAN_QUOTA_HOME": str(scratch / "quota-state"),
        "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": str(codex_bin.parent) + os.pathsep + os.environ["PATH"],
        "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "localhost,127.0.0.1",
    })
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        os.environ.pop(name, None)
    daemon_dir = state / "app-server-daemon"
    daemon_dir.mkdir()
    (daemon_dir / "settings.json").write_text(json.dumps({
        "remoteControlEnabled": False, "shutdownGraceSeconds": 1,
        "updater": {"autoUpdateEnabled": False},
    }), encoding="utf-8")
    auth_path = state / "auth.json"
    routing_accounts = []

    class AccountBackend(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            status = 200
            if path == "/backend-api/wham/accounts/check":
                identity = self.headers["chatgpt-account-id"]
                routing_accounts.append(identity)
                data = {"accounts": [{"id": identity, "workspace_backend_origin": "https://chatgpt.com",
                                      "account_routing_override": "NO_CONSTRAINT"}], "default_account_id": identity}
            elif path == "/backend-api/wham/config/bundle":
                data = {"requirements_toml": {"enterprise_managed": []}}
            else:
                status, data = 404, {}
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    def lifecycle(action):
        result = subprocess.run(["codex", "app-server", "daemon", action],
                                capture_output=True, timeout=60, cwd=ROOT)
        (scratch / f"{action}.stdout.log").write_bytes(result.stdout)
        (scratch / f"{action}.stderr.log").write_bytes(result.stderr)
        if result.returncode:
            print(result.stderr.decode("utf-8", errors="replace")[-4000:], file=sys.stderr, flush=True)
        result.check_returncode()
        return json.loads(result.stdout)

    def pid():
        return json.loads((daemon_dir / "daemon.pid").read_text(encoding="utf-8"))["pid"]

    def api_key(identity):
        return Account(provider="openai", label="OpenAI", source="dushan-quota", identity=identity,
                       auth_mode="api_key", secret={"api_key": "sk-synthetic-" + identity})

    def oauth(identity):
        claims = {"sub": "synthetic-user-" + identity, "email": identity + "@example.test", "exp": int(time.time()) + 7200,
                  "https://api.openai.com/auth": {"chatgpt_account_id": identity, "chatgpt_plan_type": "plus"}}
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        token = f'{encode({"alg": "RS256", "kid": "synthetic"})}.{encode(claims)}.synthetic-signature'
        return Account(provider="openai", label="OpenAI", source="dushan-quota", identity=identity,
                       auth_mode="oauth", user_id=identity, secret={"access": token, "refresh": "synthetic-refresh",
                                                                  "id_token": token, "account_id": identity})

    def daemon_account(socket_path):
        with unix_connect(socket_path, proxy=None, open_timeout=5, close_timeout=2) as connection:
            def request(request_id, method, params):
                connection.send(json.dumps({"id": request_id, "method": method, "params": params}))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    reply = json.loads(connection.recv(timeout=deadline - time.monotonic()))
                    if reply.get("id") == request_id:
                        assert "error" not in reply, reply
                        return reply["result"]
                raise TimeoutError("Codex account response timed out")

            request(1, "initialize", {"clientInfo": {"name": "quota_daemon_smoke", "version": "0.7.8"}})
            connection.send(json.dumps({"method": "initialized"}))
            return request(2, "account/read", {"refreshToken": False})["account"]

    with ExitStack() as cleanup:
        backend = cleanup.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), AccountBackend))
        worker = Thread(target=backend.serve_forever, daemon=True)
        worker.start()
        cleanup.callback(worker.join, 5)
        cleanup.callback(backend.shutdown)
        cleanup.enter_context(patch.object(provision, "_codex_auth_path", return_value=auth_path))
        (state / "config.toml").write_text(
            'cli_auth_credentials_store = "file"\n'
            f'chatgpt_base_url = "http://127.0.0.1:{backend.server_port}/backend-api/"\n', encoding="utf-8",
        )
        initial = provision.provision(oauth("A"), "codex", confirmed=True)
        assert initial["ok"] and "重启" not in initial["message"]
        try:
            started = lifecycle("start")
            assert started["status"] == "started" and started["backend"] == "pid"
            assert started["managedCodexVersion"] == "0.160.0"
            assert Path(started["managedCodexPath"]).resolve().is_relative_to(state.resolve())
            first = pid()
            assert daemon_account(started["socketPath"])["email"] == "A@example.test"
            switched = provision.provision(oauth("B"), "codex", confirmed=True)
            assert switched["ok"] and "已重启 Codex 服务" in switched["message"], switched
            second = pid()
            assert second != first
            assert json.loads(auth_path.read_text(encoding="utf-8"))["tokens"]["account_id"] == "B"
            assert daemon_account(started["socketPath"])["email"] == "B@example.test"
            switched = provision.provision(api_key("C"), "codex", confirmed=True)
            assert switched["ok"] and "已重启 Codex 服务" in switched["message"], switched
            third = pid()
            assert third != second
            assert json.loads(auth_path.read_text(encoding="utf-8"))["OPENAI_API_KEY"] == "sk-synthetic-C"
            assert daemon_account(started["socketPath"])["type"] == "apiKey"
            os.kill(third, 0)
            assert not (daemon_dir / "daemon-updater.pid").exists()
        finally:
            stopped = lifecycle("stop")
            assert stopped["status"] in {"stopped", "notRunning"}
        assert not (daemon_dir / "daemon.pid").exists()
        assert set(routing_accounts) == {"A", "B"}, routing_accounts
    result = {"platform": sys.platform, "codex_version": started["managedCodexVersion"],
              "api_key_restart": True, "oauth_restart": True, "pid_changes": [first, second, third],
              "account_file_preserved": True, "account_in_daemon_verified": True,
              "local_routing_fixture": True, "isolated_service_stopped": True}
    (scratch / "checks.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
