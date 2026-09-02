import os
import unittest
import unicodedata
from unittest.mock import patch

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
        cells = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in fitted[-2])
        self.assertLessEqual(cells, 24)
        self.assertEqual("line 9", fitted[-1])


if __name__ == "__main__":
    unittest.main()
