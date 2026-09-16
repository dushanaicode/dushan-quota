import ctypes
import runpy
import sys
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from lib import float_win


class FloatPlatformTests(unittest.TestCase):
    def test_import_and_dpi_setup_do_not_use_windows_apis_on_other_platforms(self):
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform), patch.object(
                ctypes, "WINFUNCTYPE", side_effect=AssertionError("Windows-only API"), create=True
            ) as factory:
                module = runpy.run_path(float_win.__file__, run_name="lib._float_import_test")
                module["_install_dpi_handler"](Mock())
                factory.assert_not_called()

    def test_windows_dpi_callback_is_reused_and_forwards_other_messages(self):
        comctl32 = Mock()
        comctl32.DefSubclassProc.return_value = 42
        native = SimpleNamespace(comctl32=comctl32, user32=Mock())
        with patch.object(float_win, "_is_windows", return_value=True), patch.object(
            float_win, "_hwnd", return_value=123
        ), patch.object(ctypes, "windll", native, create=True), patch.object(
            ctypes, "WINFUNCTYPE", side_effect=ctypes.CFUNCTYPE, create=True
        ) as factory, patch.object(float_win, "_dpi_proc_callback", None), patch.object(
            float_win, "_def_subclass_proc", None
        ), patch.object(float_win, "_WINDOW_DPI", {}):
            window = Mock()
            float_win._install_dpi_handler(window)
            callback = float_win._dpi_proc_callback
            self.assertIsNotNone(callback)
            self.assertEqual(42, callback(123, 0x0042, 1, 2, 0, 0))
            comctl32.DefSubclassProc.assert_called_once_with(123, 0x0042, 1, 2)
            float_win._install_dpi_handler(window)
            self.assertIs(callback, float_win._dpi_proc_callback)
            factory.assert_called_once()
            self.assertEqual(2, comctl32.SetWindowSubclass.call_count)

    def test_macos_tray_shares_the_webview_event_loop(self):
        pystray = Mock()
        application = Mock()
        appkit = SimpleNamespace(NSApplication=application)
        with patch.dict(sys.modules, {"pystray": pystray, "AppKit": appkit}), patch.object(
            float_win, "_is_macos", return_value=True
        ), patch.object(float_win, "_tray_image"), patch.object(float_win.threading, "Thread") as thread:
            tray = float_win._Tray(float_win.Api())
            tray.start()
            self.assertIs(application.sharedApplication.return_value, pystray.Icon.call_args.kwargs["darwin_nsapplication"])
            pystray.Icon.return_value.run_detached.assert_called_once_with()
            pystray.Icon.return_value.run.assert_not_called()
            thread.assert_not_called()

    def test_windows_tray_keeps_its_background_loop(self):
        pystray = Mock()
        with patch.dict(sys.modules, {"pystray": pystray}), patch.object(
            float_win, "_is_macos", return_value=False
        ), patch.object(float_win, "_tray_image"), patch.object(float_win.threading, "Thread") as thread:
            float_win._Tray(float_win.Api()).start()
            pystray.Icon.return_value.run_detached.assert_not_called()
            self.assertNotIn("darwin_nsapplication", pystray.Icon.call_args.kwargs)
            thread.assert_called_once_with(target=pystray.Icon.return_value.run, daemon=True)
            thread.return_value.start.assert_called_once_with()

    def test_tray_quit_keeps_the_event_loop_available_for_window_shutdown(self):
        api = Mock()
        tray = float_win._Tray(api)
        tray._icon = Mock()
        tray._quit()
        api.quit.assert_called_once_with()
        tray._icon.stop.assert_not_called()

    def test_macos_tray_cleanup_does_not_stop_the_shared_application(self):
        for macos in (True, False):
            with self.subTest(macos=macos), patch.object(float_win, "_is_macos", return_value=macos):
                tray = float_win._Tray(float_win.Api())
                tray._icon = Mock()
                tray.stop()
                if macos:
                    self.assertFalse(tray._icon.visible)
                    tray._icon.stop.assert_not_called()
                else:
                    tray._icon.stop.assert_called_once_with()

    def test_macos_bridge_quit_dispatches_native_termination_without_destroying_webview(self):
        api = float_win.Api()
        api._window = Mock()
        app = Mock()
        pid = Mock()
        order = Mock()
        order.attach_mock(pid.unlink, "remove_pid")
        order.attach_mock(app.terminate_, "terminate")
        queued = []
        with patch.object(float_win, "_is_macos", return_value=True), patch.object(
            float_win, "_invoke_on_ui", side_effect=lambda window, callback: queued.append(callback)
        ), patch.object(float_win, "_float_pid_path", return_value=pid), patch.dict(
            sys.modules, {"AppKit": SimpleNamespace(NSApplication=Mock(sharedApplication=Mock(return_value=app)))}
        ):
            api.quit()
            api._window.destroy.assert_not_called()
            app.terminate_.assert_not_called()
            self.assertEqual(1, len(queued))
            queued[0]()
        self.assertEqual([call.remove_pid(missing_ok=True), call.terminate(None)], order.mock_calls)

    def test_windows_bridge_quit_destroys_the_window(self):
        api = float_win.Api()
        api._window = Mock()
        with patch.object(float_win, "_is_macos", return_value=False), patch.object(float_win, "_terminate_macos") as terminate:
            api.quit()
        api._window.destroy.assert_called_once_with()
        terminate.assert_not_called()

    def test_macos_tray_refresh_does_not_synchronously_wait_for_javascript(self):
        api = float_win.Api()
        api._window = Mock()
        queued = []
        with patch.object(float_win, "_is_macos", return_value=True), patch.object(
            float_win, "_invoke_on_ui", side_effect=lambda window, callback: queued.append(callback)
        ):
            float_win._Tray(api)._refresh()
        api._window.evaluate_js.assert_not_called()
        webview = api._window.native.contentView.return_value
        webview.evaluateJavaScript_completionHandler_.assert_not_called()
        self.assertEqual(1, len(queued))
        queued[0]()
        webview.evaluateJavaScript_completionHandler_.assert_called_once_with("refresh(true)", None)

    def test_native_close_finishes_macos_application_after_cleanup(self):
        for fails in (False, True):
            with self.subTest(fails=fails), ExitStack() as patches:
                patches.enter_context(patch.object(float_win, "_is_macos", return_value=True))
                patches.enter_context(patch.object(float_win, "_is_windows", return_value=False))
                for name in ("_enable_dpi_awareness", "_mac_hide_dock_icon", "_start_embedded_web"):
                    patches.enter_context(patch.object(float_win, name))
                patches.enter_context(patch.object(float_win, "_primary_scale", return_value=1))
                patches.enter_context(patch.object(float_win.config, "apply_config_env"))
                patches.enter_context(patch.object(float_win.config, "load_config", return_value={}))
                pid = patches.enter_context(patch.object(float_win, "_float_pid_path")).return_value
                timers = [Mock(), Mock()]
                patches.enter_context(patch.object(float_win.threading, "Timer", side_effect=timers))
                patches.enter_context(patch.object(float_win.webview, "create_window", return_value=Mock()))
                start = patches.enter_context(patch.object(float_win.webview, "start"))
                tray = patches.enter_context(patch.object(float_win, "_Tray")).return_value
                terminate = patches.enter_context(patch.object(float_win, "_terminate_macos"))
                order = Mock()
                order.attach_mock(tray.stop, "tray_cleanup")
                order.attach_mock(pid.unlink, "remove_pid")
                order.attach_mock(terminate, "terminate")
                if fails:
                    start.side_effect = RuntimeError("GUI startup failed")
                    with self.assertRaisesRegex(RuntimeError, "GUI startup failed"):
                        float_win.serve_float()
                    terminate.assert_not_called()
                else:
                    float_win.serve_float()
                    self.assertEqual([
                        call.tray_cleanup(), call.remove_pid(missing_ok=True), call.terminate(),
                    ], order.mock_calls)
                for timer in timers:
                    timer.start.assert_called_once_with()
                    timer.cancel.assert_called_once_with()

    def test_macos_window_changes_are_dispatched_to_the_main_thread(self):
        foundation = SimpleNamespace(NSThread=Mock())
        foundation.NSThread.isMainThread.return_value = False
        helper = Mock()
        queued = []
        helper.callAfter.side_effect = queued.append
        window = Mock()
        with patch.dict(sys.modules, {
            "Foundation": foundation, "PyObjCTools": SimpleNamespace(AppHelper=helper),
        }), patch.object(float_win, "_is_macos", return_value=True):
            float_win._set_alpha(window, 82)
            float_win._set_topmost(window, False)
            window.native.setAlphaValue_.assert_not_called()
            window.native.setLevel_.assert_not_called()
            self.assertEqual(2, len(queued))
            for call in queued:
                call()
            window.native.setAlphaValue_.assert_called_once_with(0.82)
            window.native.setLevel_.assert_called_once_with(0)

    def test_macos_ui_callback_runs_immediately_on_the_main_thread(self):
        foundation = SimpleNamespace(NSThread=Mock())
        foundation.NSThread.isMainThread.return_value = True
        helper = Mock()
        callback = Mock()
        with patch.dict(sys.modules, {
            "Foundation": foundation, "PyObjCTools": SimpleNamespace(AppHelper=helper),
        }), patch.object(float_win, "_is_macos", return_value=True):
            float_win._invoke_on_ui(Mock(), callback)
            callback.assert_called_once_with()
            helper.callAfter.assert_not_called()

    def test_macos_drag_and_resize_dispatch_each_position_to_the_ui_thread(self):
        for resize in (False, True):
            with self.subTest(resize=resize):
                event = Mock()
                event.pressedMouseButtons.side_effect = [1, 1, 0]
                event.mouseLocation.side_effect = [
                    SimpleNamespace(x=100, y=200),
                    SimpleNamespace(x=130, y=170),
                    SimpleNamespace(x=150, y=150),
                ]
                api = float_win.Api()
                api._window = Mock()
                native = api._window.native
                native.frame.return_value = SimpleNamespace(
                    origin=SimpleNamespace(x=10, y=20),
                    size=SimpleNamespace(width=290, height=430),
                )
                queued = []
                with patch.dict(sys.modules, {"AppKit": SimpleNamespace(NSEvent=event)}), patch.object(
                    float_win, "_invoke_on_ui", side_effect=lambda window, call: queued.append(call)
                ), patch.object(float_win.time, "sleep"), patch.object(api, "_persist_size"):
                    self.assertEqual({"ok": True}, api._mac_track(resize=resize))
                native.setFrame_display_.assert_not_called()
                native.setFrameOrigin_.assert_not_called()
                self.assertEqual(2, len(queued))
                for call in queued:
                    call()
                if resize:
                    self.assertEqual(
                        [(((10, -10), (320, 460)), True), (((10, -30), (340, 480)), True)],
                        [call.args for call in native.setFrame_display_.call_args_list],
                    )
                else:
                    self.assertEqual(
                        [((40, -10),), ((60, -30),)],
                        [call.args for call in native.setFrameOrigin_.call_args_list],
                    )


if __name__ == "__main__":
    unittest.main()
