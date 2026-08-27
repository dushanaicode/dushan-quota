import http.client
import io
import json
import socket
import unittest
import urllib.error
from unittest.mock import call, patch

from lib import httputil


class _Response:
    def __init__(self, *, status=200, body=b'{}', headers=None, read_error=None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.read_error = read_error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        if self.read_error is not None:
            raise self.read_error
        return self.body

    def close(self):
        pass


def _http_error(status, body=b'{}', headers=None):
    return urllib.error.HTTPError(
        "https://example.test/quota",
        status,
        "test error",
        headers or {},
        io.BytesIO(body),
    )


class RequestJsonRetryTests(unittest.TestCase):
    @patch.object(httputil.time, "sleep")
    @patch.object(httputil.urllib.request, "urlopen")
    def test_get_retries_incomplete_read_then_succeeds(self, urlopen, sleep):
        urlopen.side_effect = [
            _Response(read_error=http.client.IncompleteRead(b"x" * 1030, 478)),
            _Response(body=b'{"ok": true}'),
        ]

        status, text, data = httputil.request_json("https://example.test/quota")

        self.assertEqual(200, status)
        self.assertEqual({"ok": True}, data)
        self.assertEqual({"ok": True}, json.loads(text))
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(0.3)

    @patch.object(httputil.time, "sleep")
    @patch.object(httputil.urllib.request, "urlopen")
    def test_get_stops_after_two_retries(self, urlopen, sleep):
        urlopen.side_effect = [
            _Response(read_error=http.client.IncompleteRead(b"first", 3)),
            _Response(read_error=http.client.IncompleteRead(b"second", 2)),
            _Response(read_error=http.client.IncompleteRead(b"last", 1)),
        ]

        status, text, data = httputil.request_json("https://example.test/quota")

        self.assertEqual(0, status)
        self.assertIn("IncompleteRead", text)
        self.assertIn("重试 2 次后仍失败", text)
        self.assertIsNone(data)
        self.assertEqual(3, urlopen.call_count)
        self.assertEqual([call(0.3), call(1.0)], sleep.call_args_list)

    @patch.object(httputil.time, "sleep")
    @patch.object(httputil.urllib.request, "urlopen")
    def test_post_never_retries_transport_error(self, urlopen, sleep):
        urlopen.return_value = _Response(
            read_error=http.client.IncompleteRead(b"partial", 12)
        )

        status, _, data = httputil.request_json(
            "https://example.test/consume",
            method="POST",
            body={"redeem_request_id": "fixed-id"},
        )

        self.assertEqual(0, status)
        self.assertIsNone(data)
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()
        request = urlopen.call_args.args[0]
        self.assertEqual("POST", request.get_method())
        self.assertEqual({"redeem_request_id": "fixed-id"}, json.loads(request.data))

    @patch.object(httputil.time, "sleep")
    @patch.object(httputil.urllib.request, "urlopen")
    def test_post_never_retries_http_503(self, urlopen, sleep):
        urlopen.side_effect = _http_error(503, b'{"error": "busy"}')

        status, _, data = httputil.request_json(
            "https://example.test/consume",
            method="POST",
            body={"redeem_request_id": "fixed-id"},
        )

        self.assertEqual(503, status)
        self.assertEqual({"error": "busy"}, data)
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

    @patch.object(httputil.time, "sleep")
    @patch.object(httputil.urllib.request, "urlopen")
    def test_401_is_returned_without_http_layer_retry(self, urlopen, sleep):
        urlopen.side_effect = _http_error(401, b'{"error": "unauthorized"}')

        status, _, data = httputil.request_json("https://example.test/quota")

        self.assertEqual(401, status)
        self.assertEqual({"error": "unauthorized"}, data)
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

    @patch.object(httputil.time, "sleep")
    @patch.object(httputil.urllib.request, "urlopen")
    def test_get_retries_429_and_honors_retry_after(self, urlopen, sleep):
        urlopen.side_effect = [
            _http_error(429, b'{"error": "slow down"}', {"Retry-After": "2"}),
            _Response(body=b'{"ok": true}'),
        ]

        status, _, data = httputil.request_json("https://example.test/quota")

        self.assertEqual(200, status)
        self.assertEqual({"ok": True}, data)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(2.0)

    @patch.object(httputil.time, "sleep")
    @patch.object(httputil.urllib.request, "urlopen")
    def test_get_does_not_retry_when_retry_after_exceeds_cap(self, urlopen, sleep):
        urlopen.side_effect = _http_error(
            429,
            b'{"error": "slow down"}',
            {"Retry-After": "31"},
        )

        status, _, data = httputil.request_json("https://example.test/quota")

        self.assertEqual(429, status)
        self.assertEqual({"error": "slow down"}, data)
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

    @patch.object(httputil.time, "time", return_value=10.0)
    @patch.object(httputil.time, "sleep")
    @patch.object(httputil.urllib.request, "urlopen")
    def test_get_honors_http_date_retry_after(self, urlopen, sleep, _time):
        urlopen.side_effect = [
            _http_error(
                503,
                b'{"error": "busy"}',
                {"Retry-After": "Thu, 01 Jan 1970 00:00:12 GMT"},
            ),
            _Response(body=b'{"ok": true}'),
        ]

        status, _, data = httputil.request_json("https://example.test/quota")

        self.assertEqual(200, status)
        self.assertEqual({"ok": True}, data)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(2.0)

    @patch.object(httputil.time, "sleep")
    @patch.object(httputil.urllib.request, "urlopen")
    def test_get_retries_only_temporary_dns_errors(self, urlopen, sleep):
        urlopen.side_effect = [
            urllib.error.URLError(
                socket.gaierror(socket.EAI_AGAIN, "temporary name resolution failure")
            ),
            _Response(body=b'{"ok": true}'),
        ]

        status, _, data = httputil.request_json("https://example.test/quota")

        self.assertEqual(200, status)
        self.assertEqual({"ok": True}, data)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(0.3)

    @patch.object(httputil.time, "sleep")
    @patch.object(httputil.urllib.request, "urlopen")
    def test_get_does_not_retry_permanent_dns_errors(self, urlopen, sleep):
        urlopen.side_effect = urllib.error.URLError(
            socket.gaierror(socket.EAI_NONAME, "name does not exist")
        )

        status, _, data = httputil.request_json("https://example.test/quota")

        self.assertEqual(0, status)
        self.assertIsNone(data)
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

    @patch.object(httputil.time, "sleep")
    @patch.object(httputil.urllib.request, "urlopen")
    def test_get_retries_http_error_whose_body_is_incomplete(self, urlopen, sleep):
        error = _http_error(503)
        error.fp = _Response(
            read_error=http.client.IncompleteRead(b"partial", 5)
        )
        urlopen.side_effect = [error, _Response(body=b'{"ok": true}')]

        status, _, data = httputil.request_json("https://example.test/quota")

        self.assertEqual(200, status)
        self.assertEqual({"ok": True}, data)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(0.3)


if __name__ == "__main__":
    unittest.main()
