import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from lib import provision
from lib.models import Account


def _jwt(payload: dict) -> str:
    encode = lambda value: base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


class GrokCliProvisionTests(unittest.TestCase):
    def setUp(self):
        temp_root = (Path.cwd() / "Temp").resolve()
        temp_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.temporary.name).resolve()
        self.assertEqual(temp_root, self.root.parent)
        self.auth_path = self.root / "auth.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_writes_required_create_time_and_jwt_expiry(self):
        issued = 1_788_310_829
        expires = issued + 21_600
        access = _jwt({"iat": issued, "exp": expires, "sub": "grok-user"})
        account = Account(
            provider="grok",
            label="Grok",
            source="dushan-quota",
            identity="grok-user",
            auth_mode="oauth",
            user_id="grok-user",
            secret={"access": access, "refresh": "refresh-token", "expires": expires + 9_999},
        )

        with (
            patch.object(provision, "_grok_cli_path", return_value=self.auth_path),
            patch.object(provision.agentdb, "record_provision"),
        ):
            result = provision._write_grok_cli(account, confirmed=True)

        self.assertTrue(result["ok"], result)
        entry = json.loads(self.auth_path.read_text(encoding="utf-8"))[
            f"{provision.XAI_ISSUER}::{provision.XAI_CLIENT_ID}"
        ]
        self.assertEqual(
            datetime.fromtimestamp(issued, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            entry["create_time"],
        )
        self.assertEqual(
            datetime.fromtimestamp(expires, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            entry["expires_at"],
        )


if __name__ == "__main__":
    unittest.main()
