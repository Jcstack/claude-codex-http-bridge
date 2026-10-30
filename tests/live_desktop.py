"""Observe/act on an owned ordinary app only. No model/account/user windows."""

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_adapter
import desktop_scope
import runtime_bundle
from desktop_fixture import fixture


def state_metadata(result):
    for block in result.content:
        if block.type == "text":
            try:
                value = json.loads(block.text)
                if isinstance(value, dict) and "window" in value: return value
            except ValueError: pass
    raise RuntimeError("Scoped observation metadata missing")


def element(tree, label):
    for line in tree.splitlines():
        if label.lower() in line.lower():
            match = re.search(r"^\s*(?:\[([^\]]+)\]|(\d+))\s", line)
            if match: return match.group(1) or match.group(2)
    raise RuntimeError("Fixture element absent from actual accessibility tree")


async def exercise(root, work, state, value, keyboard=False):
    identity = desktop_scope.inventory().identity(value["id"])
    assert identity["pid"] == value["pid"]
    selection = {"format": 1, "enabled": True, "workspace": str(work.resolve()), "identity": identity}
    runtime_bundle.write_file(root / "desktop.json", runtime_bundle.canonical(selection), 0o600)
    session = desktop_adapter.DesktopSession(root, work)
    try:
        observed = await session.observe()
        metadata = state_metadata(observed)
        tree = metadata.get("accessibility", {}).get("tree", "")
        field = element(tree, "Fixture input")
        receipt = session.receipt[0]
        await session.act(receipt, "set_value", {"element_index": field, "value": "DESKTOP_FIXTURE_READY"})
        try: await session.act(receipt, "set_value", {"element_index": field, "value": "MUST_NOT_REPLAY"})
        except desktop_scope.ScopeError: pass
        else: raise AssertionError("An observation was reused")
        expected = "DESKTOP_FIXTURE_READY"
        if keyboard:
            observed = await session.observe()
            view = state_metadata(observed)
            await session.act(session.receipt[0], "click", {"x": view["image_width"]*0.5,
                "y": view["image_height"]*(0.38 if sys.platform == "darwin" else 0.25)})
            await session.observe()
            await session.act(session.receipt[0], "press_key", {"key": "cmd+a" if sys.platform == "darwin" else "ctrl+a"})
            await session.observe()
            expected = "UNICODE_FIXTURE_READY_😀"
            await session.act(session.receipt[0], "type_text", {"text": expected})
        observed = await session.observe()
        button = element(state_metadata(observed).get("accessibility", {}).get("tree", ""), "Save fixture")
        await session.act(session.receipt[0], "click", {"element_index": button})
        current = json.loads(state.read_text())
        if keyboard and (current["value"] != expected or current["count"] != 1):
            print(json.dumps({"synthetic_fixture_text": current["value"], "button_invocations": current["count"], "keyboard_pass": False}))
        assert current["value"] == expected and current["count"] == 1, "Native UI state did not change exactly once"
        selection["enabled"] = False
        runtime_bundle.write_file(root / "desktop.json", runtime_bundle.canonical(selection), 0o600)
        try: await session.observe()
        except desktop_scope.ScopeError: pass
        else: raise AssertionError("Disabled scope remained observable")
        print(json.dumps({"platform": sys.platform, "owned_window_only": True, "native_screenshot": "pass", "native_accessibility": "pass",
            "actual_text_change": "pass", "actual_button_action": "pass", "one_use_observation": "pass", "immediate_disable": "pass",
            "literal_unicode_keyboard": "pass" if keyboard else "not_run", "global_input": False}))
    finally: await session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--keyboard", action="store_true", help="Also exercise targeted coordinate click, select-all and Unicode text in the owned window")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="bridge-scoped-desktop-") as temporary:
        root = Path(temporary); runtime_bundle.private_dir(root)
        work = root / "workspace"; runtime_bundle.private_dir(work)
        desktop_adapter.install(root, args.archive)
        with fixture() as (_process, state, value): asyncio.run(exercise(root, work, state, value, args.keyboard))


if __name__ == "__main__": main()
