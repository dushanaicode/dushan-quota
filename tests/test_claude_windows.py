import unittest

from lib.providers.claude import _windows
from lib.render import _window_name


def _window(utilization, resets_at=None):
    return {
        "utilization": utilization,
        "resets_at": resets_at,
        "limit_dollars": None,
        "used_dollars": None,
        "remaining_dollars": None,
        "locked_reason": None,
    }


# Shape returned by api/oauth/usage: two live windows, a pile of unreleased
# per-model windows left null, and internal codename buckets that share the
# window shape.
PAYLOAD = {
    "five_hour": _window(2.0, "2026-09-12T17:30:00.665627+00:00"),
    "seven_day": _window(0.0, "2026-09-14T02:00:00.665647+00:00"),
    "seven_day_oauth_apps": None,
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "seven_day_cowork": None,
    "seven_day_omelette": None,
    "tangelo": None,
    "iguana_necktie": None,
    "omelette_promotional": None,
    "nimbus_quill": _window(0.0),
    "cinder_cove": None,
    "juniper_tide": None,
    "extra_usage": {"is_enabled": False, "utilization": None, "monthly_limit": None},
    "limits": [
        {"kind": "session", "group": "session", "percent": 2, "is_active": True,
         "resets_at": "2026-09-12T17:30:00.665627+00:00"},
        {"kind": "weekly_all", "group": "weekly", "percent": 0, "is_active": False,
         "resets_at": "2026-09-14T02:00:00.665647+00:00"},
    ],
    "spend": {"percent": 0, "enabled": False, "limit": None},
    "member_dashboard_available": False,
    "seven_day_breakdown": None,
}


class ClaudeWindowTests(unittest.TestCase):
    def test_live_payload_yields_each_window_once(self):
        windows = _windows(PAYLOAD)
        self.assertEqual(["5h quota", "Week quota"], [w.name for w in windows])
        self.assertEqual(["5 小时额度", "周额度"], [_window_name(w.name) for w in windows])
        self.assertEqual([98.0, 100.0], [w.remaining_percent for w in windows])
        self.assertEqual("2026-09-14T02:00:00.665647+00:00", windows[1].reset_iso)

    def test_codename_buckets_never_become_windows(self):
        payload = dict(PAYLOAD, tangelo=_window(88.0), juniper_tide=_window(12.0))
        self.assertNotIn("nimbus_quill", [w.name for w in _windows(payload)])
        self.assertEqual(2, len(_windows(payload)))

    def test_per_model_and_extra_windows_appear_once_enabled(self):
        payload = dict(
            PAYLOAD,
            seven_day_opus=_window(41.5, "2026-09-14T02:00:00+00:00"),
            seven_day_haiku=_window(1.0, "2026-09-14T02:00:00+00:00"),
            extra_usage={"is_enabled": True, "utilization": 25.0},
        )
        labels = [_window_name(w.name) for w in _windows(payload)]
        self.assertEqual(
            ["5 小时额度", "周额度", "Opus 周额度", "额外用量", "haiku 周额度"], labels
        )

    def test_limits_array_covers_payloads_without_top_level_keys(self):
        payload = {
            "nimbus_quill": _window(0.0),
            "limits": [
                {"kind": "session", "percent": 7, "resets_at": "2026-09-12T17:30:00+00:00"},
                {"kind": "weekly_all", "percent": 22, "resets_at": "2026-09-14T02:00:00+00:00"},
                {"kind": "weekly_opus", "percent": 60, "resets_at": "2026-09-14T02:00:00+00:00"},
            ],
        }
        windows = _windows(payload)
        self.assertEqual(["5h quota", "Week quota", "Opus Week quota"], [w.name for w in windows])
        self.assertEqual([93.0, 78.0, 40.0], [w.remaining_percent for w in windows])


if __name__ == "__main__":
    unittest.main()
