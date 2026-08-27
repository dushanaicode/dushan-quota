import os
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import quota
from lib.models import Account, QuotaResult, Window
from lib.render import display_width, render, render_loading, strip_ansi
from lib.snapshot import Snapshot
from lib.terminal import (
    ALT_SCREEN_OFF,
    ALT_SCREEN_ON,
    CLEAR_HOME,
    CURSOR_HIDE,
    CURSOR_SHOW,
    TerminalScreen,
    fit_frame,
)


class FakeStream:
    def __init__(self, tty: bool, fail_writes: int = 0):
        self.tty = tty
        self.fail_writes = fail_writes
        self.parts = []
        self.flush_count = 0

    def isatty(self):
        return self.tty

    def write(self, value):
        self.parts.append(value)
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise OSError("simulated write failure")
        return len(value)

    def flush(self):
        self.flush_count += 1

    @property
    def value(self):
        return "".join(self.parts)


def sample_result() -> QuotaResult:
    account = Account(
        provider="openai",
        label="OpenAI",
        source="codex-local",
        identity="account-1",
        auth_mode="oauth",
        email="user@example.com",
    )
    return QuotaResult(
        account=account,
        ok=True,
        title="OpenAI",
        email="user@example.com",
        name="示例账号",
        user_id="account-1",
        plan="OpenAI Pro",
        auth_mode="oauth",
        sub_start="2030-01-02T03:04:05+00:00",
        sub_end="2030-02-03T04:05:06+00:00",
        windows=[
            Window(name="Week quota", remaining_percent=63, reset_iso="2030-02-01T00:00:00+00:00"),
            Window(name="5h quota", remaining_percent=21, used=79.0, total=100.0),
            Window(name="重置次数", text="剩余 1 次"),
        ],
    )


class TerminalScreenTests(unittest.TestCase):
    def test_live_screen_uses_alternate_buffer_and_replaces_each_frame(self):
        stream = FakeStream(True)

        with TerminalScreen(stream, live=True, ansi=True, columns=80, lines=24) as screen:
            screen.draw("first frame")
            screen.draw("second frame")

        output = stream.value
        self.assertEqual(1, output.count(ALT_SCREEN_ON))
        self.assertEqual(1, output.count(ALT_SCREEN_OFF))
        self.assertEqual(1, output.count(CURSOR_HIDE))
        self.assertEqual(1, output.count(CURSOR_SHOW))
        self.assertEqual(3, output.count(CLEAR_HOME))
        self.assertGreater(output.rfind("second frame"), output.rfind(CLEAR_HOME))

    def test_non_tty_is_plain_static_output(self):
        stream = FakeStream(False)

        with TerminalScreen(stream, live=True, ansi=True) as screen:
            self.assertFalse(screen.live)
            self.assertFalse(screen.color)
            screen.draw("plain frame")

        self.assertEqual("plain frame\n", stream.value)
        self.assertNotIn("\033", stream.value)

    def test_no_color_disables_style_but_keeps_live_controls(self):
        stream = FakeStream(True)
        with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            with TerminalScreen(stream, live=True, ansi=True) as screen:
                self.assertTrue(screen.live)
                self.assertFalse(screen.color)

    def test_tiny_terminal_disables_live_mode_instead_of_wrapping(self):
        stream = FakeStream(True)

        with TerminalScreen(stream, live=True, ansi=True, columns=19, lines=3) as screen:
            self.assertFalse(screen.live)
            screen.draw("one snapshot")

        self.assertNotIn(ALT_SCREEN_ON, stream.value)
        self.assertEqual("one snapshot\n", stream.value)

    def test_failed_alternate_screen_entry_best_effort_restores_terminal(self):
        stream = FakeStream(True, fail_writes=1)
        screen = TerminalScreen(stream, live=True, ansi=True)

        with self.assertRaises(OSError):
            screen.start()

        self.assertIn(CURSOR_SHOW, stream.value)
        self.assertIn(ALT_SCREEN_OFF, stream.value)
        self.assertFalse(screen._started)

    @patch("lib.terminal._clear_windows_viewport", return_value=False)
    def test_native_clear_failure_stops_live_output(self, clear_viewport):
        stream = FakeStream(True)
        screen = TerminalScreen(stream, live=True, ansi=False)
        screen._started = True
        screen.live = True
        screen.native_clear = True
        screen._windows_state = {}

        written = screen.draw("must not append")

        self.assertFalse(written)
        self.assertFalse(screen.live)
        self.assertEqual("", stream.value)

    def test_fit_frame_preserves_header_and_footer(self):
        frame = "\n".join(f"line {index}" for index in range(10))

        fitted = fit_frame(frame, 5, 24).splitlines()

        self.assertEqual(5, len(fitted))
        self.assertEqual("line 0", fitted[0])
        self.assertIn("折叠", fitted[-2])
        self.assertLessEqual(display_width(fitted[-2]), 24)
        self.assertEqual("line 9", fitted[-1])


class TerminalRenderTests(unittest.TestCase):
    def test_loading_frame_never_exceeds_narrow_width(self):
        for width in (20, 32, 40):
            with self.subTest(width=width):
                output = render_loading(width=width, color=False)
                for line in output.splitlines():
                    self.assertLessEqual(display_width(line), width, line)

    def test_dashboard_is_plain_responsive_and_complete(self):
        output = render(
            [sample_result()],
            now=datetime(2029, 1, 1, tzinfo=timezone.utc),
            width=72,
            color=False,
        )

        self.assertNotIn("\033", output)
        self.assertIn("QUOTA", output)
        self.assertIn("OpenAI", output)
        self.assertIn("Week quota", output)
        self.assertIn("63%", output)
        self.assertIn("2030-01-02", output)
        self.assertIn("ID account-1", output)
        self.assertIn("79/100", output)
        for line in output.splitlines():
            self.assertLessEqual(display_width(line), 72, line)

    def test_compact_dashboard_surfaces_lowest_window(self):
        output = render(
            [sample_result()],
            now=datetime(2029, 1, 1, tzinfo=timezone.utc),
            width=60,
            color=False,
            compact=True,
        )

        self.assertIn("5h quota", output)
        self.assertIn("21%", output)
        self.assertIn("+2", output)
        self.assertNotIn("Week quota", output)

    def test_compact_dashboard_keeps_nine_accounts_inside_24_rows(self):
        results = []
        for index in range(9):
            results.append(
                QuotaResult(
                    account=Account(
                        provider=f"provider-{index}",
                        label=f"Provider {index}",
                        source="test",
                        identity=f"account-{index}",
                    ),
                    ok=True,
                    title=f"Provider {index}",
                    windows=[Window(name="Week quota", remaining_percent=90 - index)],
                )
            )

        output = render(results, width=79, color=False, compact=True, footer="Ctrl+C 返回")

        self.assertLessEqual(len(output.splitlines()), 23)
        for index in range(9):
            self.assertIn(f"Provider {index}", output)

    def test_color_output_can_be_stripped_without_losing_content(self):
        colored = render([sample_result()], width=80, color=True)

        self.assertIn("\033", colored)
        self.assertIn("OpenAI", strip_ansi(colored))

    def test_unavailable_balance_counts_as_attention_not_healthy(self):
        result = QuotaResult(
            account=Account(provider="deepseek", label="DeepSeek", source="quota-cli", identity="key-1"),
            ok=True,
            title="DeepSeek",
            plan="不可用（余额不足或欠费）",
            windows=[Window(name="Balance", text="¥-0.92")],
        )

        output = render([result], width=80, color=False, compact=True)

        self.assertIn("0 正常", output)
        self.assertIn("1 注意", output)
        self.assertIn("! DeepSeek", output)

    def test_stale_snapshot_uses_real_fetch_time_and_label(self):
        output = render(
            [sample_result()],
            now=datetime(2029, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2028, 5, 6, 7, 8, 9, tzinfo=timezone.utc),
            snapshot_state="stale",
            width=90,
            color=False,
        )

        self.assertIn("更新 2028-05-06", output)
        self.assertIn("旧快照", output)

    @patch.object(quota.time, "sleep")
    @patch.object(quota, "get_snapshot")
    def test_redirected_watch_emits_one_plain_snapshot(self, get_snapshot, sleep):
        get_snapshot.return_value = Snapshot(results=[sample_result()], fetched_at=1.0, from_cache=False)
        stream = FakeStream(False)

        with patch.object(quota.sys, "stdout", stream):
            quota.watch(15)

        get_snapshot.assert_called_once_with(force=False)
        sleep.assert_not_called()
        self.assertNotIn("\033", stream.value)
        self.assertIn("OpenAI", stream.value)

    @patch.object(quota.time, "sleep")
    @patch.object(quota, "get_snapshot")
    def test_live_watch_restores_terminal_after_interrupt(self, get_snapshot, sleep):
        shared = Snapshot(results=[sample_result()], fetched_at=1.0, from_cache=False)
        get_snapshot.side_effect = [shared, KeyboardInterrupt()]
        stream = FakeStream(True)

        with patch.object(quota.sys, "stdout", stream), patch(
            "quota.TerminalScreen",
            lambda output, live: TerminalScreen(output, live=live, ansi=True, columns=80, lines=30),
        ):
            quota.watch(1)

        self.assertIn(ALT_SCREEN_ON, stream.value)
        self.assertIn(ALT_SCREEN_OFF, stream.value)
        self.assertIn(CURSOR_SHOW, stream.value)
        sleep.assert_called_once_with(1)

    @patch.object(quota.time, "sleep")
    @patch.object(quota, "get_snapshot")
    def test_watch_exits_when_native_refresh_can_no_longer_clear(self, get_snapshot, sleep):
        get_snapshot.return_value = Snapshot(results=[sample_result()], fetched_at=1.0, from_cache=False)
        stream = FakeStream(True)

        class FailingScreen:
            live = True
            color = False
            width = 80
            height = 30

            def __init__(self, output, live):
                self.output = output

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def draw(self, frame):
                return False

        with patch.object(quota.sys, "stdout", stream), patch("quota.TerminalScreen", FailingScreen):
            quota.watch(1)

        get_snapshot.assert_not_called()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
