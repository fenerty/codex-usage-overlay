"""Focused offline tests for window recovery and bounded UI diagnostics.

The candidate is imported without invoking main(). All native calls and Tk roots
are fakes; these tests never enumerate or alter a real desktop window.
"""

import ctypes
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


APP_PATH = Path(__file__).with_name("codex_usage_overlay.pyw")
MODULE_NAME = "codex_usage_overlay_window_recovery_tests"
LOADER = importlib.machinery.SourceFileLoader(MODULE_NAME, str(APP_PATH))
SPEC = importlib.util.spec_from_loader(MODULE_NAME, LOADER)
overlay = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = overlay
LOADER.exec_module(overlay)


@contextmanager
def isolated_log_directory():
    """Keep synthetic logs and their verified cleanup in the artifact root."""
    root = Path(__file__).resolve().parent
    temporary = tempfile.TemporaryDirectory(prefix="window-log-test-", dir=root)
    try:
        path = Path(temporary.name).resolve()
        path.relative_to(root)
        yield str(path)
    finally:
        Path(temporary.name).resolve().relative_to(root)
        temporary.cleanup()


class WindowEventLogTests(unittest.TestCase):
    def test_invalid_retention_limits_cannot_create_an_unbounded_log(self):
        for size, backups in ((0, 2), (-1, 2), (256, 0), (256, -1)):
            with self.subTest(size=size, backups=backups):
                with self.assertRaises(ValueError):
                    overlay.WindowEventLog(max_bytes=size, backups=backups)

    def test_records_only_allowlisted_state_and_numeric_error_details(self):
        with isolated_log_directory() as directory:
            path = Path(directory) / "events.jsonl"
            log = overlay.WindowEventLog(path=path)
            error = PermissionError(13, "PRIVATE error text /private/user/path")
            error.winerror = 5
            self.assertTrue(log.write(
                "window_query_error", reason="poll", shown=True,
                mode="visible_window", error=error,
                state={"hwnd": 123, "native_visible": True, "topmost": False,
                       "rect": [1, 2, 101, 42], "window_title": "PRIVATE title",
                       "source_path": "/private/session.jsonl",
                       "token_counter": {"total": 1234}, "account": "PRIVATE"},
            ))
            raw = path.read_text(encoding="utf-8")
            record = json.loads(raw)
            self.assertEqual(record["event"], "window_query_error")
            self.assertEqual(record["mode"], "visible_window")
            self.assertTrue(record["shown"])
            self.assertEqual(set(record["native"]),
                             {"hwnd", "native_visible", "topmost", "rect"})
            self.assertEqual(record["error"],
                             {"type": "PermissionError", "errno": 13, "winerror": 5})
            self.assertNotIn("PRIVATE", raw)
            self.assertNotIn("/private", raw)
            self.assertNotIn("token_counter", raw)
            self.assertNotIn("source_path", raw)

    def test_unsupported_visibility_mode_is_not_logged(self):
        with isolated_log_directory() as directory:
            path = Path(directory) / "events.jsonl"
            log = overlay.WindowEventLog(path=path)
            self.assertTrue(log.write("window_state", mode="private arbitrary text"))
            self.assertNotIn("mode", json.loads(path.read_text(encoding="utf-8")))

    def test_identical_errors_are_suppressed_for_sixty_seconds(self):
        with isolated_log_directory() as directory:
            path = Path(directory) / "events.jsonl"
            log = overlay.WindowEventLog(path=path)
            with mock.patch.object(overlay.time, "monotonic", side_effect=[100, 159, 160]):
                results = [log.write("topmost_error", reason="native_repair",
                                     error=OSError(5, "not recorded")) for _ in range(3)]
            self.assertEqual(results, [True, False, True])
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_distinct_error_codes_are_not_lost_to_duplicate_suppression(self):
        with isolated_log_directory() as directory:
            path = Path(directory) / "events.jsonl"
            log = overlay.WindowEventLog(path=path)
            with mock.patch.object(overlay.time, "monotonic", return_value=100):
                self.assertTrue(log.write("topmost_error", error=OSError(5, "one")))
                self.assertTrue(log.write("topmost_error", error=OSError(6, "two")))
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_rotation_retains_only_bounded_current_and_two_backups(self):
        with isolated_log_directory() as directory:
            path = Path(directory) / "events.jsonl"
            log = overlay.WindowEventLog(path=path, max_bytes=256, backups=2)
            for number in range(12):
                self.assertTrue(log.write(f"sample_{number}"))
            files = [path, path.with_name("events.jsonl.1"),
                     path.with_name("events.jsonl.2")]
            self.assertTrue(all(item.exists() for item in files))
            self.assertTrue(all(item.stat().st_size <= 256 for item in files))
            self.assertFalse(path.with_name("events.jsonl.3").exists())
            records = [json.loads(line) for item in files
                       for line in item.read_text(encoding="utf-8").splitlines()]
            self.assertIn("sample_11", [record["event"] for record in records])
            self.assertNotIn("sample_0", [record["event"] for record in records])

    def test_oversized_record_does_not_expand_the_log(self):
        with isolated_log_directory() as directory:
            path = Path(directory) / "events.jsonl"
            log = overlay.WindowEventLog(path=path, max_bytes=256, backups=2)
            self.assertFalse(log.write("state", reason="x" * 1024))
            self.assertEqual(log.last_error, "RecordTooLarge")
            self.assertFalse(path.exists())

    def test_storage_denial_is_nonfatal_and_subsequent_write_can_recover(self):
        with isolated_log_directory() as directory:
            path = Path(directory) / "events.jsonl"
            log = overlay.WindowEventLog(path=path)
            with mock.patch.object(Path, "open", side_effect=PermissionError(13, "denied")):
                self.assertFalse(log.write("window_state"))
            self.assertEqual(log.last_error, "PermissionError")
            self.assertTrue(log.write("window_state"))
            self.assertIsNone(log.last_error)


class FakeTkRoot:
    def __init__(self):
        self.attribute_calls = []
        self.mapped = True
        self.frame = 0x101
        self._w = "."
        self.tk = SimpleNamespace(call=lambda *_args: hex(self.frame))

    def attributes(self, *args):
        self.attribute_calls.append(args)

    def winfo_ismapped(self):
        return self.mapped


class FakeNativeApi:
    def __init__(self):
        self.owner_pid = os.getpid()
        self.valid = True
        self.topmost = False
        self.visible = True
        self.iconic = False
        self.set_succeeds = True
        self.apply_style = True
        self.set_calls = []
        self.last_root = None

    def GetAncestor(self, child, _flags):
        self.last_root = int(child) + 0x10000
        return self.last_root

    def IsWindow(self, _hwnd):
        return self.valid

    def GetWindowThreadProcessId(self, hwnd, result):
        if result is not None:
            result._obj.value = self.owner_pid if int(hwnd) == self.last_root else 6789
        return 12345

    def GetWindowRect(self, _hwnd, result):
        result._obj.left, result._obj.top = 10, 20
        result._obj.right, result._obj.bottom = 210, 60
        return True

    def GetForegroundWindow(self):
        return 0x222

    def GetWindow(self, _hwnd, _command):
        return self.last_root

    def IsWindowVisible(self, _hwnd):
        return self.visible

    def IsIconic(self, _hwnd):
        return self.iconic

    def SetWindowPos(self, *args):
        self.set_calls.append(args)
        if self.set_succeeds and self.apply_style:
            self.topmost = True
        return self.set_succeeds


class NativeTopmostTests(unittest.TestCase):
    def make_native(self):
        native = object.__new__(overlay.WindowsOverlayWindow)
        native.root = FakeTkRoot()
        native.user32 = FakeNativeApi()
        native.dwmapi = None
        native.kernel32 = SimpleNamespace(GetCurrentThreadId=lambda: 12345)
        native.current_thread_id = lambda: 12345
        native.get_style = lambda hwnd, _index: (
            8 if int(hwnd) == native.user32.last_root and native.user32.topmost else 0
        )
        self.enterContext(mock.patch.object(overlay.ctypes, "set_last_error", create=True))
        self.enterContext(mock.patch.object(overlay.ctypes, "get_last_error",
                                           return_value=0, create=True))
        return native

    def test_topmost_repair_uses_only_no_focus_no_move_no_resize_flags(self):
        native = self.make_native()
        state = native.ensure_topmost()
        self.assertTrue(state["topmost"])
        self.assertEqual(len(native.user32.set_calls), 1)
        hwnd, insert_after, x, y, width, height, flags = native.user32.set_calls[0]
        self.assertEqual(hwnd, native.user32.last_root)
        self.assertEqual(insert_after.value, ctypes.wintypes.HWND(-1).value)
        self.assertEqual((x, y, width, height), (0, 0, 0, 0))
        self.assertEqual(flags, 0x213)
        self.assertFalse(flags & 0x40, "SWP_SHOWWINDOW must not override visibility policy")

    def test_wrong_process_ownership_prevents_any_window_change(self):
        native = self.make_native()
        native.user32.owner_pid = os.getpid() + 10000
        with self.assertRaisesRegex(OSError, "ownership"):
            native.ensure_topmost()
        self.assertEqual(native.user32.set_calls, [])

    def test_invalid_handle_prevents_any_window_change(self):
        native = self.make_native()
        native.user32.valid = False
        with self.assertRaisesRegex(OSError, "mapped"):
            native.ensure_topmost()
        self.assertEqual(native.user32.set_calls, [])

    def test_wrong_thread_prevents_any_window_change(self):
        native = self.make_native()
        native.current_thread_id = lambda: 99999
        with self.assertRaisesRegex(OSError, "thread"):
            native.ensure_topmost()
        self.assertEqual(native.user32.set_calls, [])

    def test_wrapper_handle_is_resolved_again_after_a_remap(self):
        native = self.make_native()
        native.ensure_topmost()
        native.root.frame = 0x202
        native.ensure_topmost()
        first, second = native.user32.set_calls
        self.assertNotEqual(first[0], second[0])
        self.assertEqual(second[0], 0x10202)

    def test_successful_api_return_requires_verified_topmost_style(self):
        native = self.make_native()
        native.user32.apply_style = False
        with self.assertRaisesRegex(OSError, "verification"):
            native.ensure_topmost()
        self.assertEqual(len(native.user32.set_calls), 1)

    def test_failed_native_api_is_reported_without_retrying(self):
        native = self.make_native()
        native.user32.set_succeeds = False
        with mock.patch.object(overlay.ctypes, "WinError", create=True,
                               return_value=OSError(5, "denied")):
            with self.assertRaises(OSError):
                native.ensure_topmost()
        self.assertEqual(len(native.user32.set_calls), 1)


class FakeNativeWindow:
    def __init__(self, topmost=True):
        self.state = {"hwnd": 123, "native_visible": True, "iconic": False,
                      "cloaked": 0, "topmost": topmost, "rect": [0, 0, 190, 40]}
        self.snapshot_calls = 0
        self.repair_calls = 0
        self.repair_expected_orders = []
        self.query_error = None
        self.repair_error = None

    def snapshot(self):
        self.snapshot_calls += 1
        if self.query_error is not None:
            raise self.query_error
        return dict(self.state)

    def ensure_topmost(self, expected_order=None):
        self.repair_calls += 1
        self.repair_expected_orders.append(expected_order)
        if self.repair_error is not None:
            raise self.repair_error
        if expected_order is not None:
            if overlay.window_order_repair_key(self.state) != expected_order:
                raise overlay.WindowOrderChanged("Synthetic window pair changed")
            self.state["above_foreground"] = True
        self.state["topmost"] = True
        return dict(self.state)


class TopmostIntegrationTests(unittest.TestCase):
    def make_app(self, topmost=True):
        app = object.__new__(overlay.OverlayApp)
        app.root = FakeTkRoot()
        app.settings = {"visibility_mode": "visible_window"}
        app._native_window = FakeNativeWindow(topmost)
        app._window_log = mock.Mock()
        app._overlay_is_shown = True
        app._topmost_enabled = True
        app._quitting = False
        app.menu_active = False
        app.is_dragging = False
        app._post_menu_visibility_after_id = None
        app._post_drag_visibility_after_id = None
        app._last_native_window_state = None
        app._last_topmost_error = None
        app._next_window_check_at = 0.0
        app._next_window_log_at = 0.0
        return app

    def test_native_drift_is_repaired_despite_cached_true(self):
        app = self.make_app(topmost=False)
        app._check_window_state_if_due(10)
        self.assertEqual(app._native_window.repair_calls, 1)
        self.assertTrue(app._native_window.state["topmost"])
        self.assertTrue(app._last_native_window_state["topmost"])
        self.assertEqual(app.root.attribute_calls, [])
        self.assertIsNone(app._last_topmost_error)

    def test_healthy_window_is_not_reordered_on_periodic_polls(self):
        app = self.make_app()
        app._check_window_state_if_due(10)
        app._check_window_state_if_due(15)
        self.assertEqual(app._native_window.snapshot_calls, 2)
        self.assertEqual(app._native_window.repair_calls, 0)
        self.assertEqual(app.root.attribute_calls, [])

    def test_periodic_check_is_throttled_but_force_bypasses_due_time(self):
        app = self.make_app()
        app._check_window_state_if_due(10)
        app._check_window_state_if_due(14.9)
        self.assertEqual(app._native_window.snapshot_calls, 1)
        app._check_window_state_if_due(14.9, force=True)
        self.assertEqual(app._native_window.snapshot_calls, 2)
        self.assertEqual(app._native_window.repair_calls, 0)

    def test_force_topmost_reasserts_even_when_both_cache_and_style_are_true(self):
        app = self.make_app()
        app._set_topmost(True, force=True, reason="show")
        self.assertEqual(app.root.attribute_calls, [("-topmost", True)])
        self.assertEqual(app._native_window.repair_calls, 1)

    def test_hidden_quitting_and_interaction_leases_skip_native_checks(self):
        cases = {"hidden": {"_overlay_is_shown": False},
                 "quitting": {"_quitting": True},
                 "menu": {"menu_active": True},
                 "drag": {"is_dragging": True},
                 "menu_lease": {"_post_menu_visibility_after_id": "pending"},
                 "drag_lease": {"_post_drag_visibility_after_id": "pending"}}
        for name, values in cases.items():
            with self.subTest(name=name):
                app = self.make_app(topmost=False)
                for key, value in values.items():
                    setattr(app, key, value)
                app._check_window_state_if_due(10, force=True)
                self.assertEqual(app._native_window.snapshot_calls, 0)
                self.assertEqual(app._native_window.repair_calls, 0)
                self.assertEqual(app.root.attribute_calls, [])

    def test_native_hidden_iconic_or_cloaked_windows_are_not_raised_by_poll(self):
        for name, value in (("native_visible", False), ("iconic", True), ("cloaked", 1)):
            with self.subTest(name=name):
                app = self.make_app(topmost=False)
                app._native_window.state[name] = value
                app._check_window_state_if_due(10)
                self.assertEqual(app._native_window.repair_calls, 0)

    def test_unmapped_tk_root_is_not_repaired(self):
        app = self.make_app(topmost=False)
        app.root.mapped = False
        app._set_topmost(True, force=True)
        self.assertEqual(app._native_window.repair_calls, 0)

    def test_native_query_failure_is_nonfatal_and_identified(self):
        app = self.make_app()
        app._native_window.query_error = OSError(5, "private text")
        app._check_window_state_if_due(10)
        self.assertEqual(app._last_topmost_error, "OSError")
        self.assertEqual(app._native_window.repair_calls, 0)
        self.assertEqual(app._window_log.write.call_args.args[0], "window_query_error")

    def test_native_repair_failure_is_nonfatal_and_identified(self):
        app = self.make_app(topmost=False)
        app._native_window.repair_error = PermissionError(13, "private text")
        app._check_window_state_if_due(10)
        self.assertEqual(app._last_topmost_error, "PermissionError")
        self.assertEqual(app._native_window.repair_calls, 1)
        self.assertEqual(app._window_log.write.call_args.args[0], "topmost_error")

    def test_diagnostic_writer_failure_does_not_block_recovery(self):
        app = self.make_app(topmost=False)
        app._window_log.write.side_effect = PermissionError(13, "unwritable log")
        app._check_window_state_if_due(10)
        self.assertEqual(app._native_window.repair_calls, 1)
        self.assertTrue(app._native_window.state["topmost"])
        self.assertIsNone(app._last_topmost_error)

    def test_unsupported_native_backend_preserves_tk_fallback(self):
        app = self.make_app()
        app._native_window = None
        app._topmost_enabled = False
        app._set_topmost(True)
        self.assertEqual(app.root.attribute_calls, [("-topmost", True)])
        self.assertTrue(app._topmost_enabled)


def ordinary_foreground_order_problem(**changes):
    state = {
        "hwnd": 123,
        "native_visible": True,
        "iconic": False,
        "cloaked": 0,
        "topmost": True,
        "above_foreground": False,
        "foreground_hwnd": 456,
        "foreground_pid": os.getpid() + 10000,
        "foreground_topmost": False,
        "rect": [0, 0, 190, 40],
    }
    state.update(changes)
    return state


class WindowOrderEligibilityTests(unittest.TestCase):
    def test_positive_order_problem_produces_window_pair_identity(self):
        state = ordinary_foreground_order_problem()
        self.assertEqual(overlay.window_order_repair_key(state),
                         (state["hwnd"], state["foreground_hwnd"], state["foreground_pid"]))

    def test_unknown_healthy_hidden_and_self_foregrounds_are_ineligible(self):
        cases = [
            {"native_visible": False}, {"native_visible": None},
            {"iconic": True}, {"iconic": None},
            {"cloaked": 1}, {"cloaked": None},
            {"topmost": False}, {"topmost": None},
            {"above_foreground": True}, {"above_foreground": None},
            {"foreground_topmost": True}, {"foreground_topmost": None},
            {"hwnd": 0}, {"foreground_hwnd": 0},
            {"foreground_hwnd": 123}, {"foreground_pid": 0},
            {"foreground_pid": os.getpid()},
        ]
        self.assertIsNone(overlay.window_order_repair_key(None))
        for values in cases:
            with self.subTest(values=values):
                self.assertIsNone(overlay.window_order_repair_key(
                    ordinary_foreground_order_problem(**values)))


class NativeRelativeOrderTests(unittest.TestCase):
    def make_chain(self, mapping, invalid=()):
        native = object.__new__(overlay.WindowsOverlayWindow)
        lookup = mapping if callable(mapping) else lambda hwnd: mapping.get(hwnd, 0)
        native.user32 = SimpleNamespace(
            GetWindow=mock.Mock(side_effect=lambda hwnd, _command: lookup(hwnd)),
            IsWindow=lambda hwnd: hwnd not in invalid,
        )
        return native

    def test_own_above_foreground_requires_a_positive_forward_find(self):
        native = self.make_chain({202: 303, 303: 101, 101: 0})
        self.assertIs(native._relative_order(101, 202), True)

    def test_own_below_foreground_requires_a_positive_inverse_find(self):
        native = self.make_chain({202: 0, 101: 303, 303: 202})
        self.assertIs(native._relative_order(101, 202), False)

    def test_null_cycle_and_invalid_chains_remain_unknown(self):
        cases = (
            ({202: 0, 101: 0}, ()),
            ({202: 303, 303: 202, 101: 404, 404: 101}, ()),
            ({202: 303, 101: 404}, (303, 404)),
        )
        for mapping, invalid in cases:
            with self.subTest(mapping=mapping, invalid=invalid):
                native = self.make_chain(mapping, invalid)
                self.assertIsNone(native._relative_order(101, 202))
                self.assertLessEqual(native.user32.GetWindow.call_count, 6)
        native = self.make_chain({})
        self.assertIsNone(native._relative_order(101, 0))
        self.assertIs(native._relative_order(101, 101), True)
        native.user32.GetWindow.assert_not_called()

    def test_traversal_limit_is_unknown_and_bounded_in_both_directions(self):
        native = self.make_chain(lambda hwnd: hwnd + 1)
        self.assertIsNone(native._relative_order(1000, 1))
        self.assertEqual(native.user32.GetWindow.call_count, 256)


class NativeOrderingGuardTests(unittest.TestCase):
    def make_native(self):
        native = NativeTopmostTests.make_native(self)
        state = ordinary_foreground_order_problem(
            hwnd=0x10101, foreground_hwnd=0x222, foreground_pid=6789)
        native.user32.topmost = True
        return native, state

    def test_changed_fresh_pair_or_pid_cancels_before_native_set(self):
        for field, value in (("hwnd", 0x10102), ("foreground_hwnd", 0x223),
                             ("foreground_pid", 6790)):
            with self.subTest(field=field):
                native, state = self.make_native()
                expected = overlay.window_order_repair_key(state)
                changed = dict(state, **{field: value})
                native.snapshot = mock.Mock(return_value=changed)
                with self.assertRaises(overlay.WindowOrderChanged):
                    native.ensure_topmost(expected_order=expected)
                self.assertEqual(native.user32.set_calls, [])

    def test_identity_change_after_fresh_snapshot_cancels_before_native_set(self):
        for change in ("foreground", "wrapper"):
            with self.subTest(change=change):
                native, state = self.make_native()
                expected = overlay.window_order_repair_key(state)
                native.snapshot = mock.Mock(return_value=state)
                if change == "foreground":
                    native.user32.GetForegroundWindow = lambda: state["foreground_hwnd"] + 1
                else:
                    native.root.frame += 1
                with self.assertRaises(overlay.WindowOrderChanged):
                    native.ensure_topmost(expected_order=expected)
                self.assertEqual(native.user32.set_calls, [])

    def test_confirmed_ordering_repair_retains_no_focus_geometry_flags(self):
        native, state = self.make_native()
        expected = overlay.window_order_repair_key(state)
        native.snapshot = mock.Mock(side_effect=[state, dict(state, above_foreground=True)])
        result = native.ensure_topmost(expected_order=expected)
        self.assertTrue(result["above_foreground"])
        self.assertEqual(len(native.user32.set_calls), 1)
        args = native.user32.set_calls[0]
        self.assertEqual(args[0], expected[0])
        self.assertEqual(args[1].value, ctypes.wintypes.HWND(-1).value)
        self.assertEqual(args[2:6], (0, 0, 0, 0))
        self.assertEqual(args[6], 0x213)

    def test_snapshot_marks_identity_races_unknown(self):
        for change in ("foreground", "wrapper", "vanished_foreground"):
            with self.subTest(change=change):
                native, _state = self.make_native()
                if change == "foreground":
                    native.user32.GetForegroundWindow = mock.Mock(side_effect=[0x222, 0x333])
                elif change == "wrapper":
                    native.root.tk.call = mock.Mock(side_effect=["0x101", "0x202"])
                else:
                    native.user32.IsWindow = lambda hwnd: hwnd != 0x222
                self.assertIsNone(native.snapshot()["above_foreground"])
                self.assertEqual(native.user32.set_calls, [])


class WindowOrderIntegrationTests(unittest.TestCase):
    def make_app(self):
        app = TopmostIntegrationTests.make_app(self)
        app._native_window.state = ordinary_foreground_order_problem()
        app._pending_window_order = None
        app._pending_window_order_at = 0.0
        app._next_window_order_repair_at = 0.0
        return app

    @staticmethod
    def logged_events(app):
        return [call.args[0] for call in app._window_log.write.call_args_list]

    def test_two_spaced_samples_repair_cached_topmost_without_tk_changes(self):
        app = self.make_app()
        app._topmost_enabled = False  # Even an inconsistent cache cannot cause a Tk toggle.
        expected = overlay.window_order_repair_key(app._native_window.state)
        app._check_window_state_if_due(100)
        self.assertEqual(app._native_window.repair_calls, 0)
        self.assertIn("window_order_suspect", self.logged_events(app))
        app._check_window_state_if_due(105)
        self.assertEqual(app._native_window.repair_calls, 1)
        self.assertEqual(app._native_window.repair_expected_orders, [expected])
        self.assertEqual(app.root.attribute_calls, [])
        self.assertIn("window_order_restored", self.logged_events(app))

    def test_forced_polls_cannot_confirm_same_instant_or_under_five_seconds(self):
        app = self.make_app()
        for now in (100, 100, 104.9):
            app._check_window_state_if_due(now, force=True)
            self.assertEqual(app._native_window.repair_calls, 0)
        app._check_window_state_if_due(105, force=True)
        self.assertEqual(app._native_window.repair_calls, 1)

    def test_healthy_unknown_and_topmost_foreground_reset_confirmation(self):
        for values in ({"above_foreground": True}, {"above_foreground": None},
                       {"foreground_topmost": None}, {"foreground_topmost": True},
                       {"cloaked": None}):
            with self.subTest(values=values):
                app = self.make_app()
                original = dict(app._native_window.state)
                app._check_window_state_if_due(100)
                app._native_window.state.update(values)
                app._check_window_state_if_due(105)
                app._native_window.state = original
                app._check_window_state_if_due(110)
                self.assertEqual(app._native_window.repair_calls, 0)
                app._check_window_state_if_due(115)
                self.assertEqual(app._native_window.repair_calls, 1)

    def test_unknown_backend_and_query_errors_break_confirmation(self):
        for failure in ("none_snapshot", "no_backend", "query_error"):
            with self.subTest(failure=failure):
                app = self.make_app()
                native = app._native_window
                app._check_window_state_if_due(100)
                if failure == "none_snapshot":
                    with mock.patch.object(native, "snapshot", return_value=None):
                        app._check_window_state_if_due(105)
                elif failure == "no_backend":
                    app._native_window = None
                    app._check_window_state_if_due(105)
                    app._native_window = native
                else:
                    native.query_error = OSError(5, "synthetic query failure")
                    app._check_window_state_if_due(105)
                    native.query_error = None
                app._check_window_state_if_due(110)
                self.assertEqual(native.repair_calls, 0)
                app._check_window_state_if_due(115)
                self.assertEqual(native.repair_calls, 1)

    def test_changed_window_or_foreground_identity_requires_new_confirmation(self):
        for field in ("hwnd", "foreground_hwnd", "foreground_pid"):
            with self.subTest(field=field):
                app = self.make_app()
                app._check_window_state_if_due(100)
                app._native_window.state[field] += 1
                expected = overlay.window_order_repair_key(app._native_window.state)
                app._check_window_state_if_due(105)
                self.assertEqual(app._native_window.repair_calls, 0)
                app._check_window_state_if_due(110)
                self.assertEqual(app._native_window.repair_expected_orders, [expected])

    def test_hidden_and_interaction_gates_clear_a_pending_order_problem(self):
        gates = {"hidden": ("_overlay_is_shown", False, True),
                 "quitting": ("_quitting", True, False),
                 "menu": ("menu_active", True, False),
                 "drag": ("is_dragging", True, False),
                 "menu_lease": ("_post_menu_visibility_after_id", "pending", None),
                 "drag_lease": ("_post_drag_visibility_after_id", "pending", None)}
        for name, (field, active, inactive) in gates.items():
            with self.subTest(name=name):
                app = self.make_app()
                app._check_window_state_if_due(100)
                calls_before = app._native_window.snapshot_calls
                setattr(app, field, active)
                app._check_window_state_if_due(105, force=True)
                self.assertEqual(app._native_window.snapshot_calls, calls_before)
                self.assertEqual(app._native_window.repair_calls, 0)
                setattr(app, field, inactive)
                app._check_window_state_if_due(110)
                self.assertEqual(app._native_window.repair_calls, 0)
                app._check_window_state_if_due(115)
                self.assertEqual(app._native_window.repair_calls, 1)

    def test_cooldown_applies_to_successful_and_failed_attempts(self):
        for fail_first in (False, True):
            with self.subTest(fail_first=fail_first):
                app = self.make_app()
                if fail_first:
                    app._native_window.repair_error = PermissionError(13, "synthetic failure")
                app._check_window_state_if_due(100)
                app._check_window_state_if_due(105)
                self.assertEqual(app._native_window.repair_calls, 1)
                app._native_window.state["above_foreground"] = False
                app._native_window.repair_error = None
                for now in (110, 115, 130):
                    app._check_window_state_if_due(now)
                self.assertEqual(app._native_window.repair_calls, 1)
                app._check_window_state_if_due(135)
                self.assertEqual(app._native_window.repair_calls, 1)
                app._check_window_state_if_due(140)
                self.assertEqual(app._native_window.repair_calls, 2)

    def test_current_snapshot_change_cancels_confirmed_stale_pair(self):
        app = self.make_app()
        state = dict(app._native_window.state)
        app._check_window_state_if_due(100)
        changed = dict(state, foreground_hwnd=state["foreground_hwnd"] + 1)
        with mock.patch.object(app._native_window, "snapshot", side_effect=[state, changed]):
            app._check_window_state_if_due(105)
        self.assertEqual(app._native_window.repair_calls, 0)
        self.assertEqual(app.root.attribute_calls, [])
        self.assertIn("window_order_cancelled", self.logged_events(app))
        self.assertNotIn("window_order_restored", self.logged_events(app))

    def test_race_reported_by_native_guard_is_cancellation_not_failure(self):
        app = self.make_app()
        app._native_window.repair_error = overlay.WindowOrderChanged("synthetic race")
        app._check_window_state_if_due(100)
        app._check_window_state_if_due(105)
        self.assertEqual(app._native_window.repair_calls, 1)
        self.assertIn("window_order_cancelled", self.logged_events(app))
        self.assertNotIn("topmost_error", self.logged_events(app))
        self.assertIsNone(app._last_topmost_error)

    def test_unverified_readback_is_never_reported_as_restored(self):
        for changes in ({"above_foreground": False}, {"above_foreground": None},
                        {"topmost": False}, {"foreground_hwnd": 457},
                        {"foreground_pid": 1}, {"hwnd": 124}):
            with self.subTest(changes=changes):
                app = self.make_app()
                result = dict(app._native_window.state, above_foreground=True)
                result.update(changes)
                app._native_window.ensure_topmost = mock.Mock(return_value=result)
                app._check_window_state_if_due(100)
                app._check_window_state_if_due(105)
                app._native_window.ensure_topmost.assert_called_once()
                self.assertIn("window_order_unresolved", self.logged_events(app))
                self.assertNotIn("window_order_restored", self.logged_events(app))

    def test_lost_topmost_bit_keeps_existing_immediate_repair_during_order_cooldown(self):
        app = self.make_app()
        app._next_window_order_repair_at = 999
        app._native_window.state["topmost"] = False
        app._check_window_state_if_due(100)
        self.assertEqual(app._native_window.repair_calls, 1)
        self.assertEqual(app._native_window.repair_expected_orders, [None])

    def test_log_storage_failure_does_not_block_order_recovery(self):
        app = self.make_app()
        app._window_log.write.side_effect = PermissionError(13, "synthetic log denial")
        app._check_window_state_if_due(100)
        app._check_window_state_if_due(105)
        self.assertEqual(app._native_window.repair_calls, 1)
        self.assertTrue(app._native_window.state["above_foreground"])
        self.assertEqual(app.root.attribute_calls, [])


if __name__ == "__main__":
    unittest.main()
