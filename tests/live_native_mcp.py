"""Real Claude calls native image/edit/PDF/Realtime via the installed MCP pack.

Consumes subscription quota. Only an isolated synthetic workspace/config is used.
Raw transcript, pictures, audio and local paths never appear in the report.
Existing services are neither restarted nor reloaded.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
import capabilities


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(); root = args.root.resolve()
    capabilities.selected_python(root); capabilities.selected_server(root)
    services = [root/"runtime/service.json", root/"runtime-managed/service.json"]
    before = {path: path.read_bytes() for path in services if path.exists()}
    with tempfile.TemporaryDirectory(prefix="bridge-live-native-mcp-") as temporary:
        base = Path(temporary); base.chmod(0o700)
        work = base/"workspace"; work.mkdir(); config = base/"claude"; config.mkdir(mode=0o700)
        writer = PdfWriter(); page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject(); stream.set_data(b"BT /F1 12 Tf 10 100 Td (PDF_VIOLET_73) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream); writer.write(work/"fixture.pdf")
        names = ("image_models", "image_generate", "image_edit", "document_extract", "realtime_voice")
        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP") if k in os.environ}
        env.update({"CLAUDE_CODEX_BRIDGE_HOME": str(root), "CLAUDE_CONFIG_DIR": str(config), "ENABLE_TOOL_SEARCH": "true",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"})
        prompt = ("Run this isolated integration check using only the codex_native MCP tools, loading them through ToolSearch as needed. "
            "1. Extract fixture.pdf and retain its exact marker. 2. List image models, then generate one plain solid blue square, white background, no text, "
            "using gpt-image-2.5, low quality, 1024x1024, exact_size true. 3. Edit the artifact returned by that same image_generate call into a plain solid red square, "
            "using gpt-image-2.5, low quality, 1024x1024, exact_size true. Use the returned same-session artifact path directly. "
            "4. Call realtime_voice with the prompt 'Say only: violet seventy three', max_seconds 30. Do not play audio or capture a microphone. "
            "Do not use file or shell tools or substitute another implementation. Report the PDF marker and whether every tool succeeded. "
            "If any native tool errors, report that error and stop; do not automatically repeat media generation.")
        command = [sys.executable, str(root/"launcher.py"), "--native-tools", "--model", "sol", "--effort", "high",
            "--restricted", "--strict-mcp-config", "--tools", "ToolSearch", "--allowedTools", "ToolSearch",
            *("mcp__codex_native__"+name for name in names), "--permission-mode", "dontAsk", "--max-turns", "16",
            "--output-format", "stream-json", "--verbose", "-p", prompt]
        started = time.monotonic()
        run = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=300)
        if run.returncode: raise RuntimeError("Live native MCP Claude process failed; raw output withheld")
        events = []
        for line in run.stdout.splitlines():
            try: events.append(json.loads(line))
            except ValueError: pass
        final = next((e for e in reversed(events) if e.get("type") == "result"), {})
        uses, results = {}, {}
        for event in events:
            for block in event.get("message", {}).get("content", []) if isinstance(event.get("message", {}).get("content"), list) else []:
                if block.get("type") == "tool_use": uses[block["id"]] = block["name"]
                if block.get("type") == "tool_result": results[block["tool_use_id"]] = block
        called = list(uses.values())
        for name in names:
            tool = "mcp__codex_native__"+name
            calls = [key for key, value in uses.items() if value == tool]
            if len(calls) != 1 or results.get(calls[0], {}).get("is_error") or calls[0] not in results:
                print(json.dumps({"subscription_only": True, "stage": name, "success": False,
                    "called_tools": sorted(set(called)), "raw_output_withheld": True}))
                raise SystemExit(1)
        metadata = {}
        for key, tool in uses.items():
            content = results.get(key, {}).get("content", [])
            if isinstance(content, str): content = [{"type": "text", "text": content}]
            for block in content:
                if block.get("type") == "text":
                    try:
                        value = json.loads(block["text"])
                        if isinstance(value, dict): metadata[tool.rsplit("__",1)[-1]] = value
                    except ValueError: pass
        for name in ("image_generate", "image_edit"):
            value = metadata[name]
            with Image.open(value["artifact"]) as image: assert image.size == (1024,1024)
            assert Path(value["native_artifact"]).is_file()
        voice = metadata["realtime_voice"]
        assert voice["audio_seconds"] > 0 and Path(voice["audio_artifact"]).is_file() and voice["microphone_used"] is False
        assert "PDF_VIOLET_73" in final.get("result", "") and not final.get("is_error")
        assert all(path.read_bytes() == value for path, value in before.items()), "Existing service state changed"
        print(json.dumps({"subscription_only": True, "real_claude": True, "tool_search": "ToolSearch" in called,
            "image_generate": "pass", "image_edit_same_session_artifact": "pass", "pdf_extract": "pass", "realtime_voice": "pass",
            "voice_model": voice["served_model"], "voice_seconds": voice["audio_seconds"], "services_unchanged": True,
            "image_delivery_sizes": {name: metadata[name]["delivered_size"] for name in ("image_generate", "image_edit")},
            "native_image_sizes": {name: metadata[name]["native_size"] for name in ("image_generate", "image_edit")},
            "seconds": round(time.monotonic()-started,3)}))


if __name__ == "__main__": main()
