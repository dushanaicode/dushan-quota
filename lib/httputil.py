import json
import urllib.error
import urllib.request


def request_json(url, method="GET", headers=None, body=None, timeout=20):
    data = None
    req_headers = dict(headers or {})
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        data = raw
        req_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8", errors="replace")
        return error.code, payload, _try_json(payload)
    except Exception as error:
        return 0, str(error), None
    return status, payload, _try_json(payload)


def _try_json(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
