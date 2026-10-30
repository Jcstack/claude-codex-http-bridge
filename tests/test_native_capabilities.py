"""Capability protocols and privacy; synthetic providers only, never accounts."""

import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from native_client import NativeClient, NativeRouteError
from native_services import CapabilityInputError, NativeServices
import capabilities
import runtime_bundle

try:
    from mcp import Client
    from PIL import Image, PngImagePlugin
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    import httpx2
    from native_tools import create_server
    EXTRAS = True
except ImportError:
    EXTRAS = False


class LocalScopeTests(unittest.TestCase):
    def test_unsafe_routes_are_rejected_before_any_key_is_sent(self):
        client = NativeClient.__new__(NativeClient)
        for path in ("https://trap.invalid/v1/models", "//trap.invalid/v1/models", "/v1/../management",
                     "/v1/%2e%2e/management", "/v1/%5cmanagement", "/v1/models#fragment", "/v1/\nmodels"):
            with self.assertRaises(NativeRouteError): client.validate_path(path)

    def test_inputs_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); work = root / "workspace"; work.mkdir()
            outside = root / "outside.pdf"; outside.write_bytes(b"SYNTHETIC_PRIVATE_VALUE")
            services = NativeServices(root, work, client=object())
            for value in (str(outside), "../outside.pdf"):
                with self.assertRaises(CapabilityInputError): services.read_file(value)
            if hasattr(Path, "symlink_to"):
                try: (work / "link.pdf").symlink_to(outside)
                except OSError: return
                with self.assertRaises(CapabilityInputError): services.read_file("link.pdf")

    def test_source_snapshot_survives_helper_updates_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runtime_bundle.private_dir(root)
            for name in capabilities.SERVER_SOURCES: runtime_bundle.write_file(root/name, b"# fixture\n", 0o700)
            lock = "a" * 64
            identity = capabilities.freeze_server(root, lock)
            value = {"format": 1, "enabled": True, "lock": lock, "server": identity}
            runtime_bundle.write_file(root / "capabilities.json", runtime_bundle.canonical(value), 0o600)
            selected = capabilities.selected_server(root)
            runtime_bundle.write_file(root / "native_tools.py", b"# new helper version\n", 0o700)
            self.assertEqual(capabilities.selected_server(root), selected)
            self.assertNotEqual(capabilities.freeze_server(root, lock), identity)
            runtime_bundle.write_file(selected, b"# tampered\n", 0o700)
            with self.assertRaisesRegex(RuntimeError, "modified"): capabilities.selected_server(root)

    def test_native_mcp_respects_strict_profile_opt_out_and_api_separation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); ui = root / "ui"; ui.mkdir()
            for args in (["--strict-mcp-config", "--mcp-config", "user.json"], ["--no-native-tools", "-p", "fixture"]):
                flags, clean = capabilities.launch_flags(root, ui, root, list(args), "subscription")
                self.assertEqual(flags, [])
                self.assertNotIn("--no-native-tools", clean)
            with self.assertRaisesRegex(ValueError, "subscription"):
                capabilities.launch_flags(root, ui, root, ["--native-tools"], "api")
            with self.assertRaises(ValueError):
                capabilities.launch_flags(root, ui, root, ["--native-tools", "--no-native-tools"], "subscription")
            with patch.object(capabilities, "config", return_value={"enabled": True}), patch.object(capabilities, "selected_python", return_value=Path("fixture-python")), patch.object(capabilities, "selected_server", return_value=Path("fixture-server")):
                original = ["--mcp-config", "user.json", "-p", "fixture"]
                flags, clean = capabilities.launch_flags(root, ui, root, original, "subscription")
                self.assertEqual(flags, ["--mcp-config", str(ui / "native-mcp.json")])
                self.assertEqual(clean, original)
                self.assertEqual(capabilities.launch_flags(root, ui, root, ["--strict-mcp-config"], "subscription")[0], [])


@unittest.skipUnless(EXTRAS, "Optional capability lock is tested in its dedicated CI job")
class CapabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name); self.work = self.root / "workspace"; self.work.mkdir()
        output = io.BytesIO(); Image.new("RGBA", (84, 84), "blue").save(output, format="PNG")
        self.png = output.getvalue()
        self.requests = []
        self.owner = self

        class FixtureClient:
            async def async_json(inner, path, payload=None):
                self.requests.append((path, payload))
                if path == "/v1/models": return {"data": [{"id": "gpt-image-2"}, {"id": "gpt-image-9.2"}, {"id": "gpt-6.1-sol"}]}
                return {"data": [{"b64_json": base64.b64encode(self.png).decode()}]}

        self.services = NativeServices(self.root, self.work, client=FixtureClient())

    def tearDown(self): self.temporary.cleanup()

    async def test_native_image_is_preserved_and_exact_size_is_transparent(self):
        metadata, raw = await self.services.image_request("Fixture", size="64x64")
        self.assertEqual(metadata["model"], "gpt-image-9.2")
        self.assertEqual(metadata["native_size"], "84x84")
        self.assertEqual(metadata["delivered_size"], "64x64")
        self.assertTrue(metadata["resizing_applied"])
        self.assertEqual(Path(metadata["native_artifact"]).read_bytes(), self.png)
        self.assertEqual(Image.open(io.BytesIO(raw)).size, (64, 64))
        self.assertNotEqual(metadata["artifact"], metadata["native_artifact"])
        native, unchanged = await self.services.image_request("Fixture", size="64x64", exact_size=False)
        self.assertEqual(native["size_contract"], "provider_mismatch")
        self.assertEqual(unchanged, self.png)
        metadata, padded = await self.services.image_request("Fixture", size="96x64")
        self.assertTrue(metadata["padding_applied"])
        self.assertTrue(metadata["aspect_ratio_preserved"])
        image = Image.open(io.BytesIO(padded))
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertEqual(image.size, (96, 64))

    async def test_async_transport_is_local_private_tls_bounded_and_never_redirects(self):
        import ssl
        client = NativeClient.__new__(NativeClient)
        client.base = "https://127.0.0.1:12345"; client.key = "SYNTHETIC_PRIVATE_KEY"; client.context = ssl.create_default_context()
        requests = []
        actual = httpx2.AsyncClient
        options = []
        status = 200; content = b'{"data":[]}'
        def handle(request):
            requests.append(request)
            return httpx2.Response(status, content=content, headers={"Location": "https://trap.invalid/"})
        def factory(**kwargs):
            options.append(kwargs)
            return actual(**kwargs, transport=httpx2.MockTransport(handle))
        with patch.object(httpx2, "AsyncClient", side_effect=factory):
            self.assertEqual(await client.async_json("/v1/models"), {"data": []})
            self.assertFalse(options[0]["trust_env"]); self.assertFalse(options[0]["follow_redirects"])
            self.assertIs(options[0]["verify"], client.context)
            status = 302
            with self.assertRaises(NativeRouteError) as caught: await client.async_json("/v1/models")
            self.assertEqual(caught.exception.status, 302)
            self.assertTrue(all(str(request.url).startswith(client.base) for request in requests))
            status = 500; content = b'SYNTHETIC_PRIVATE_VALUE'
            with self.assertRaises(NativeRouteError) as caught: await client.async_json("/v1/models")
            self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", str(caught.exception))
            status = 200; content = b"x"*100
            with self.assertRaisesRegex(NativeRouteError, "response_limit"): await client.async_json("/v1/models", limit=50)

    async def test_edit_does_not_send_input_metadata_or_allow_path_escape(self):
        metadata = PngImagePlugin.PngInfo(); metadata.add_text("location", "SYNTHETIC_PRIVATE_VALUE")
        Image.new("RGBA", (16, 16), "red").save(self.work / "input.png", pnginfo=metadata)
        await self.services.image_request("Fixture", images=["input.png"])
        path, payload = self.requests[-1]
        self.assertEqual(path, "/v1/images/edits")
        encoded = payload["images"][0]["image_url"].split(",", 1)[1]
        image = Image.open(io.BytesIO(base64.b64decode(encoded)))
        self.assertNotIn("location", image.info)
        count = len(self.requests)
        with self.assertRaises(CapabilityInputError): await self.services.image_request("Fixture", images=["../outside.png"])
        self.assertEqual(len(self.requests), count+1)  # Catalog read only; no generation/edit request.

    async def test_mcp_returns_text_and_image_and_redacts_unexpected_errors(self):
        server = create_server(self.services)
        async with Client(server) as client:
            result = await client.call_tool("image_generate", {"prompt": "Fixture", "size": "64x64"})
            self.assertEqual([block.type for block in result.content], ["text", "image"])
            self.assertEqual(json.loads(result.content[0].text)["delivered_size"], "64x64")
            async def broken(*_args, **_kwargs): raise RuntimeError("SYNTHETIC_PRIVATE_VALUE")
            with patch.object(self.services, "image_request", side_effect=broken):
                result = await client.call_tool("image_generate", {"prompt": "Fixture"})
            text = "".join(getattr(block, "text", "") for block in result.content)
            self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", text)
            self.assertIn("withheld", text)

    async def test_pdf_extracts_actual_page_text_and_marks_empty_pages(self):
        writer = PdfWriter(); page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject(); stream.set_data(b"BT /F1 12 Tf 10 100 Td (VIOLET_73) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        writer.add_blank_page(width=200, height=200)
        writer.write(self.work / "fixture.pdf")
        value = self.services.document_extract("fixture.pdf")
        self.assertIn("VIOLET_73", value["pages"][0]["text"])
        self.assertTrue(value["pages"][1]["needs_visual_read_or_ocr"])
        self.assertFalse(value["has_other_pages"])

    async def test_realtime_is_completed_audio_and_never_replays_truncation(self):
        samples = b"\x00\x00" * 24000
        owner = self
        class Socket:
            def __init__(inner):
                inner.sent = []
                inner.events = iter([{"type": "session.created", "session": {"model": "gpt-realtime-fixture"}},
                    {"type": "session.updated", "session": {"model": "gpt-realtime-fixture"}},
                    {"type": "response.output_audio.delta", "delta": base64.b64encode(samples).decode()},
                    {"type": "response.output_audio_transcript.done", "transcript": "Fixture ready"},
                    {"type": "response.done", "response": {"status": "completed"}}])
            async def send(inner, raw): inner.sent.append(json.loads(raw))
            def __aiter__(inner): return inner
            async def __anext__(inner):
                try: return json.dumps(next(inner.events))
                except StopIteration: raise StopAsyncIteration
            async def __aenter__(inner): return inner
            async def __aexit__(inner, *_args): owner.closed = True
        socket = Socket(); self.closed = False
        self.services.client.realtime = lambda _model: socket
        result = await self.services.realtime_exchange("Fixture")
        self.assertEqual(result["served_model"], "gpt-realtime-fixture")
        self.assertEqual(result["audio_seconds"], 1)
        self.assertFalse(result["microphone_used"])
        self.assertTrue(self.closed)
        with wave.open(result["audio_artifact"]) as output: self.assertEqual(output.getnframes(), 24000)
        socket = Socket(); socket.events = iter([{"type": "session.updated"}, {"type": "response.output_audio.delta", "delta": base64.b64encode(samples).decode()}])
        self.services.client.realtime = lambda _model: socket
        with self.assertRaises(NativeRouteError): await self.services.realtime_exchange("Fixture")


if __name__ == "__main__": unittest.main()
