import unittest
from pathlib import Path


class WebOverviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1] / "lib" / "assets" / "index.html"
        ).read_text(encoding="utf-8")

    def test_overview_replaces_focus_and_upcoming_reset_labels(self):
        self.assertIn('id="btnFocus" onclick="toggleFocus(true)">总览</button>', self.html)
        self.assertIn('data-tab="low"', self.html)
        self.assertIn('data-tab="subscription"', self.html)
        self.assertNotIn('data-tab="soon"', self.html)
        self.assertNotIn("即将重置", self.html)

    def test_update_button_uses_read_only_check_endpoint(self):
        self.assertIn('id="btnUpdate" onclick="checkUpdate()">检查更新</button>', self.html)
        self.assertIn("api('/api/update-check')", self.html)

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
        self.assertIn("账号和登录信息保留", self.html)
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

    def test_supported_accounts_open_lazy_usage_detail_modal(self):
        self.assertIn('id="usageModal"', self.html)
        self.assertIn('class="btn act-usage">用量详情</button>', self.html)
        self.assertIn("api('/api/usage?' + query.toString())", self.html)
        self.assertIn("<th>模型</th><th>总 Token</th><th>输入</th>", self.html)
        self.assertIn("local?'本机':'远端'", self.html)
        self.assertIn("左右滑动查看全部列 →", self.html)

    def test_cards_show_recorded_activation_target_and_time(self):
        self.assertIn('class="activation-badge ${esc(status)}"', self.html)
        self.assertIn("active.label", self.html)
        self.assertIn("fmtActivation(active.written_at)", self.html)
        self.assertNotIn("最近写入", self.html)
        self.assertIn("已激活但过期", self.html)
        self.assertIn("已激活 · 不可续期", self.html)

    def test_usage_detail_filters_period_and_harness(self):
        self.assertIn("data-period=", self.html)
        self.assertIn("data-harness=", self.html)
        self.assertIn("aggregateLocalUsage", self.html)


if __name__ == "__main__":
    unittest.main()
