import unittest
from pathlib import Path


class WebOverviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1] / "web" / "index.html"
        ).read_text(encoding="utf-8")

    def test_overview_replaces_focus_and_upcoming_reset_labels(self):
        self.assertIn('id="btnFocus" onclick="toggleFocus(true)">总览</button>', self.html)
        self.assertIn('data-tab="low"', self.html)
        self.assertIn('data-tab="subscription"', self.html)
        self.assertNotIn('data-tab="soon"', self.html)
        self.assertNotIn("即将重置", self.html)

    def test_subscription_overview_uses_normalized_subscription_fields(self):
        self.assertIn("fmtDate(it.sub_start)", self.html)
        self.assertIn("fmtDate(it.sub_end)", self.html)
        self.assertIn("it.sub_status || ''", self.html)
        self.assertIn("时间暂不可获取", self.html)

    def test_close_button_confirms_before_saving_card_to_history(self):
        self.assertIn('id="btnHistory"', self.html)
        self.assertIn('class="x" title="关闭卡片"', self.html)
        self.assertIn('id="closeCardModal"', self.html)
        self.assertIn("关闭这张卡片？", self.html)
        self.assertIn("确认关闭", self.html)
        self.assertIn("账号、登录状态和认证凭证不会被删除", self.html)
        self.assertIn("/api/archive", self.html)
        self.assertIn("/api/restore", self.html)
        self.assertNotIn("移入历史", self.html)
        self.assertNotIn("隐藏卡片", self.html)
        self.assertNotIn("method:'DELETE'", self.html)

    def test_history_can_permanently_delete_an_account(self):
        self.assertIn("永久删除", self.html)
        self.assertIn('id="forgetCardModal"', self.html)
        self.assertIn("永久删除这个账号？", self.html)
        self.assertIn("/api/forget", self.html)
        self.assertIn("确认删除", self.html)


if __name__ == "__main__":
    unittest.main()
