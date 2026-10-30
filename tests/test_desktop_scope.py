"""Window policy failures must precede desktop dispatch; no real app accesses."""

import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

import desktop_adapter
from desktop_scope import ScopeError, MacWindows
import runtime_bundle


class DesktopPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.root = Path(self.temporary.name)
        runtime_bundle.private_dir(self.root)
        self.identity = {"id": 123, "pid": 456, "created": "1", "executable": "synthetic-fixture"}
        self.value = {"format": 1, "enabled": True, "workspace": str(self.root.resolve()), "identity": self.identity}
        self.write()
        class Scope:
            def identity(inner, _window, **_kwargs): return self.identity
        self.session = desktop_adapter.DesktopSession(self.root, self.root, scope=Scope())
        self.calls = []
        async def call(name, arguments): self.calls.append((name, arguments)); return "fixture-result"
        self.session.call = call

    def write(self): runtime_bundle.write_file(self.root / "desktop.json", runtime_bundle.canonical(self.value), 0o600)
    def receipt(self, expiry=None):
        self.session.receipt = ("fixture-receipt", expiry or time.monotonic()+60, "fixture-shot", 640, 480)
    def tearDown(self): self.temporary.cleanup()

    async def test_observation_is_one_use_even_after_dispatch_failure(self):
        self.receipt()
        result = await self.session.act("fixture-receipt", "click", {"element_index": 7})
        self.assertEqual(result["backend_dispatches"], 1)
        self.assertEqual(self.calls[0][1], {"window": {"id": 123}, "element_index": "7", "click_method": "accessibility", "screenshotId": "fixture-shot"})
        with self.assertRaises(ScopeError): await self.session.act("fixture-receipt", "click", {"element_index": "7"})
        self.assertEqual(len(self.calls), 1)
        self.receipt()
        async def broken(*_args): raise ScopeError("Synthetic failure")
        self.session.call = broken
        with self.assertRaises(ScopeError): await self.session.act("fixture-receipt", "click", {"x": 20, "y": 20})
        self.assertIsNone(self.session.receipt)

    async def test_scope_changes_disabled_expired_and_argument_escapes_never_dispatch(self):
        for arguments in ({"x": 700, "y": 10}, {"x": float("nan"), "y": 10}, {"x": 10, "y": 10, "window": {"id": 999}},
                          {"element_index": "7", "click_method": "global"}, {"x": 10, "y": 10, "app": "other-app"}):
            self.receipt()
            with self.assertRaises(ScopeError): await self.session.act("fixture-receipt", "click", arguments)
        self.receipt(time.monotonic()-1)
        with self.assertRaises(ScopeError): await self.session.act("fixture-receipt", "click", {"element_index": "7"})
        self.receipt(); self.identity = {**self.identity, "created": "2"}
        with self.assertRaises(ScopeError): await self.session.act("fixture-receipt", "click", {"element_index": "7"})
        self.value["enabled"] = False; self.write()
        with self.assertRaises(ScopeError): self.session.check()
        self.assertEqual(self.calls, [])

    async def test_only_one_bridge_session_can_lease_a_window(self):
        lease = self.session.lease()
        try:
            other = desktop_adapter.DesktopSession(self.root, self.root, scope=self.session.scope)
            with self.assertRaisesRegex(ScopeError, "leased"): other.lease()
        finally: lease.close()
        self.session.lease().close()

    async def test_child_never_inherits_model_keys_or_global_input_opt_ins(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "SYNTHETIC_PRIVATE_KEY", "ANTHROPIC_AUTH_TOKEN": "SYNTHETIC_PRIVATE_KEY",
                         "OPEN_COMPUTER_USE_ALLOW_GLOBAL_POINTER_FALLBACKS": "1"}):
            env = desktop_adapter.child_environment()
        self.assertNotIn("OPENAI_API_KEY", env); self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertEqual(env["OPEN_COMPUTER_USE_ALLOW_GLOBAL_POINTER_FALLBACKS"], "0")
        self.assertEqual(env["OPEN_COMPUTER_USE_DISABLE_APP_AGENT_PROXY"], "1")

    async def test_windows_worker_does_not_replay_uncertain_actions_or_read_other_windows(self):
        from desktop_windows import WindowsWorker
        class Scope:
            def bounds(inner, _window): return (0, 0, 640, 480)
        worker = WindowsWorker(Path("fixture"), {}, 123, Scope())
        worker.snapshot = {"windowHandle": 123, "windowBounds": {}, "elements": [{"index": 7, "name": "Fixture"}]}
        worker.bounds = (0, 0, 640, 480)
        worker.operation = AsyncMock(side_effect=ScopeError("uncertain fixture action outcome"))
        args = {"window": {"id": 123}, "element_index": "7", "click_method": "accessibility"}
        with self.assertRaises(ScopeError): await worker.call_tool("click", args)
        self.assertEqual(worker.operation.await_count, 1)
        self.assertEqual(worker.operation.call_args.args[0]["element"]["index"], 7)
        self.assertIsNone(worker.snapshot)
        with self.assertRaises(ScopeError): await worker.call_tool("click", args)
        self.assertEqual(worker.operation.await_count, 1)
        with self.assertRaises(ScopeError): await worker.call_tool("get_window_state", {"window": {"id": 999}})

    async def test_unicode_input_stops_on_focus_change_and_never_splits_surrogate_pairs(self):
        scope = MacWindows.__new__(MacWindows)
        scope.identity = lambda _window, **_kwargs: {"pid": 456}
        scope.ax = SimpleNamespace(AXUIElementCreateApplication=lambda _pid: 1)
        scope.cf = SimpleNamespace(CFRelease=lambda _value: None)
        events, data, posted, checks = [], {}, [], []
        def event(_source, _key, down):
            events.append(down); return len(events)
        def unicode_value(event_id, _length, values): data[event_id] = bytes(values)
        scope.cg = SimpleNamespace(CGEventCreateKeyboardEvent=event, CGEventSetFlags=lambda *_args: None,
            CGEventKeyboardSetUnicodeString=unicode_value, CGEventPostToPid=lambda pid, event_id: posted.append((pid,event_id)))
        def focus(_app, _window):
            checks.append(True)
            if len(checks) > 1: raise ScopeError("Synthetic focus change")
        scope.keyboard_target = focus
        with self.assertRaises(ScopeError): await scope.type_text(123, "😀"*22)
        self.assertEqual(posted, [(456,1),(456,2)])
        self.assertEqual(data[1].decode("utf-16-le"), "😀"*10)
        self.assertEqual(data[1], data[2])


if __name__ == "__main__": unittest.main()
