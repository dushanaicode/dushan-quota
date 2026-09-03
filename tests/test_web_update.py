import unittest
from unittest.mock import patch

from lib import web


class WebUpdateTests(unittest.TestCase):
    @patch.object(web, "_current_version", return_value="0.1.0")
    @patch.object(web, "request_json", return_value=(200, "", {"tag_name": "v0.2.0"}))
    def test_reports_newer_release(self, request_json, _current_version):
        result = web._update_payload()

        self.assertTrue(result["ok"])
        self.assertTrue(result["update_available"])
        self.assertEqual("0.2.0", result["latest_version"])
        self.assertEqual(web.RELEASES_URL, result["release_url"])
        request_json.assert_called_once_with(
            web.RELEASE_API,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "dushan-quota/0.1.0"},
            timeout=5,
        )

    @patch.object(web, "_current_version", return_value="0.1.0")
    @patch.object(web, "request_json", return_value=(404, "", {"message": "Not Found"}))
    def test_no_release_is_not_an_error(self, _request_json, _current_version):
        result = web._update_payload()

        self.assertTrue(result["ok"])
        self.assertFalse(result["update_available"])
        self.assertEqual("暂无已发布版本", result["message"])


if __name__ == "__main__":
    unittest.main()
