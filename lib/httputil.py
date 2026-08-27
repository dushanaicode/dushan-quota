import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime


_GET_RETRY_DELAYS = (0.3, 1.0)
_RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_MAX_RETRY_AFTER_SECONDS = 30.0


def request_json(url, method="GET", headers=None, body=None, timeout=20):
    data = None
    req_headers = dict(headers or {})
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        data = raw
        req_headers.setdefault("Content-Type", "application/json")
    normalized_method = str(method or "GET").upper()
    retry_delays = _GET_RETRY_DELAYS if normalized_method == "GET" and body is None else ()

    for attempt in range(len(retry_delays) + 1):
        request = urllib.request.Request(
            url,
            data=data,
            headers=req_headers,
            method=normalized_method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
                status = response.status
                response_headers = response.headers
        except urllib.error.HTTPError as error:
            status = error.code
            response_headers = error.headers
            try:
                payload = error.read().decode("utf-8", errors="replace")
            except Exception as read_error:
                payload = str(read_error)
            finally:
                error.close()
            if _should_retry_status(status, attempt, retry_delays, response_headers):
                continue
            return status, payload, _try_json(payload)
        except Exception as error:
            if _is_retryable_error(error) and _sleep_before_retry(attempt, retry_delays):
                continue
            message = str(error)
            if attempt:
                message = f"重试 {attempt} 次后仍失败: {message}"
            return 0, message, None

        if _should_retry_status(status, attempt, retry_delays, response_headers):
            continue
        return status, payload, _try_json(payload)

    raise AssertionError("retry loop exhausted without returning")


def _should_retry_status(status, attempt, retry_delays, headers) -> bool:
    if status not in _RETRYABLE_HTTP_STATUSES:
        return False
    return _sleep_before_retry(attempt, retry_delays, headers)


def _sleep_before_retry(attempt, retry_delays, headers=None) -> bool:
    if attempt >= len(retry_delays):
        return False
    delay = retry_delays[attempt]
    retry_after = _retry_after_seconds(headers)
    if retry_after is not None:
        if retry_after > _MAX_RETRY_AFTER_SECONDS:
            return False
        delay = max(delay, retry_after)
    time.sleep(delay)
    return True


def _retry_after_seconds(headers) -> float | None:
    if not headers:
        return None
    try:
        raw = headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        return max(0.0, parsed.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _is_retryable_error(error) -> bool:
    retryable = (
        http.client.IncompleteRead,
        http.client.RemoteDisconnected,
        http.client.BadStatusLine,
        TimeoutError,
        ConnectionError,
        socket.timeout,
        ssl.SSLEOFError,
    )
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    if isinstance(error, retryable):
        return True
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        return reason is not error and _is_retryable_error(reason)
    return False


def _try_json(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
