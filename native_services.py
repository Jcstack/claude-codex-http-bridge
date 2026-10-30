"""Scoped local tools for the Claude harness; no Codex agent is started."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import wave

from native_client import NativeClient, NativeRouteError
import runtime_bundle


MAX_FILE = 32 << 20
MAX_PIXELS = 32_000_000


class CapabilityInputError(ValueError):
    """Only fixed, public validation messages may cross the tool boundary."""


class NativeServices:
    def __init__(self, root: Path, workspace: Path, *, client=None):
        self.root = root.resolve()
        self.workspace = workspace.resolve()
        self._client = client
        self._artifacts = None
        self._produced = set()

    @property
    def client(self):
        if self._client is None:
            self._client = NativeClient(self.root)
        return self._client

    def read_file(self, name: str) -> bytes:
        path = Path(name)
        base = self.workspace
        if path.is_absolute():
            if str(path) not in self._produced or self._artifacts is None:
                raise CapabilityInputError("Use a workspace-relative path or an artifact returned by this MCP session")
            base, path = self._artifacts, Path(path.name)
        if not name or any(part == ".." for part in path.parts):
            raise CapabilityInputError("Use a workspace-relative path or an artifact returned by this MCP session")
        candidate = base / path
        for parent in (candidate, *candidate.parents):
            if parent == base: break
            if parent.is_symlink() or (os.name == "nt" and parent.exists() and getattr(parent.stat(), "st_file_attributes", 0) & 0x400):
                raise CapabilityInputError("Symlinked or reparse-point inputs are not accepted")
        resolved = candidate.resolve(strict=True)
        if base.is_symlink() or not resolved.is_relative_to(base):
            raise CapabilityInputError("Input file is outside the current workspace")
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            raise CapabilityInputError("Input must be a regular file no larger than 32 MiB")
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise CapabilityInputError("Input identity changed before reading")
            raw = handle.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE: raise CapabilityInputError("Input exceeded the file limit")
        return raw

    def artifact(self, raw: bytes, extension: str) -> str:
        if extension not in ("png", "wav"): raise CapabilityInputError("Invalid artifact format")
        if self._artifacts is None:
            runtime_bundle.private_dir(self.root / "runtime-tools")
            self._artifacts = self.root / "runtime-tools" / ("session-" + secrets.token_hex(12))
            runtime_bundle.private_dir(self._artifacts)
        path = self._artifacts / (secrets.token_hex(16) + "." + extension)
        runtime_bundle.write_file(path, raw, 0o600)
        if os.name == "nt":
            import windows_acl
            windows_acl.lock([path])
        self._produced.add(str(path))
        return str(path)

    @staticmethod
    def image(raw: bytes):
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in ("PNG", "JPEG", "WEBP") or source.width * source.height > MAX_PIXELS:
                raise CapabilityInputError("Image format or pixel count is outside the supported bounds")
            source.load()
            result = ImageOps.exif_transpose(source).convert("RGBA")
            result.info.clear()
            return result

    async def image_models(self) -> list[str]:
        value = await self.client.async_json("/v1/models")
        return sorted({row["id"] for row in value.get("data", []) if isinstance(row.get("id"), str)
                       and re.fullmatch(r"gpt-image-[a-z0-9.-]+", row["id"])})

    async def image_request(self, prompt: str, *, model: str | None = None, size: str = "auto",
                            quality: str = "auto", background: str = "auto", images: list[str] | None = None,
                            mask: str | None = None, exact_size: bool = True):
        from PIL import Image, ImageOps
        if not 1 <= len(prompt) <= 32000: raise CapabilityInputError("Prompt must contain 1–32,000 characters")
        requested = None
        if size != "auto":
            if not re.fullmatch(r"[1-9][0-9]{1,4}x[1-9][0-9]{1,4}", size):
                raise CapabilityInputError("Use auto or WIDTHxHEIGHT")
            requested = tuple(int(value) for value in size.split("x"))
            if requested[0] * requested[1] > MAX_PIXELS: raise CapabilityInputError("Requested image exceeds the pixel bound")
        if quality not in ("auto", "low", "medium", "high") or background not in ("auto", "opaque", "transparent"):
            raise CapabilityInputError("Unsupported image quality or background")
        available = await self.image_models()
        if model is None:
            plain = [value for value in available if re.fullmatch(r"gpt-image-\d+(?:\.\d+)*", value)]
            if not plain: raise CapabilityInputError("No verified unqualified image model is in the live catalog; choose an explicit listed model")
            model = max(plain, key=lambda value: tuple(map(int, value.removeprefix("gpt-image-").split("."))))
        if model not in available: raise CapabilityInputError("Requested image model is absent from the authenticated catalog")
        payload = {"model": model, "prompt": prompt, "size": size, "quality": quality,
                   "background": background, "output_format": "png", "n": 1}
        if images is not None:
            if not 1 <= len(images) <= 8: raise CapabilityInputError("Provide 1–8 workspace image files")
            encoded = []
            for name in images:
                image = self.image(self.read_file(name))
                output = io.BytesIO(); image.save(output, format="PNG")
                encoded.append({"image_url": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()})
            payload["images"] = encoded
            payload["input_fidelity"] = "high"
            if mask is not None:
                image = self.image(self.read_file(mask))
                if image.size != self.image(self.read_file(images[0])).size:
                    raise CapabilityInputError("The mask must match the first edit input's pixel dimensions")
                if image.getextrema()[3][0] == 255:
                    raise CapabilityInputError("The edit mask needs a transparent region to select pixels for editing")
                output = io.BytesIO(); image.save(output, format="PNG")
                payload["mask"] = {"image_url": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()}
        elif mask is not None: raise CapabilityInputError("A mask requires edit input images")
        if len(json.dumps(payload)) > 64 << 20: raise CapabilityInputError("Combined image input exceeds the request bound")
        value = await self.client.async_json("/v1/images/edits" if images is not None else "/v1/images/generations", payload)
        items = value.get("data", [])
        if len(items) != 1 or not isinstance(items[0].get("b64_json"), str):
            raise NativeRouteError(None, "unexpected_image_response")
        encoded = items[0]["b64_json"]
        if len(encoded) > (MAX_FILE * 4 // 3) + 4: raise NativeRouteError(None, "image_limit")
        try: raw = base64.b64decode(encoded, validate=True)
        except ValueError: raise NativeRouteError(None, "invalid_image_encoding") from None
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise NativeRouteError(None, "provider_image_format_mismatch")
        image = self.image(raw)
        native_size = image.size
        resized = requested is not None and requested != native_size and exact_size
        native_path = self.artifact(raw, "png")
        padded = False
        if resized:
            # Preserve the composition rather than silently stretching faces,
            # UI screenshots or diagrams when the provider changes the ratio.
            fitted = ImageOps.contain(image, requested, method=Image.Resampling.LANCZOS)
            padded = fitted.size != requested
            image = Image.new("RGBA", requested, "white" if background == "opaque" else (0, 0, 0, 0))
            image.paste(fitted, ((requested[0]-fitted.width)//2, (requested[1]-fitted.height)//2))
            output = io.BytesIO(); image.save(output, format="PNG"); delivered = output.getvalue()
            path = self.artifact(delivered, "png")
        else:
            delivered, path = raw, native_path
        return {"model": model, "artifact": path, "native_artifact": native_path,
                "requested_size": size, "native_size": f"{native_size[0]}x{native_size[1]}",
                "delivered_size": f"{image.width}x{image.height}", "resizing_applied": resized,
                "resize_algorithm": "lanczos" if resized else None,
                "aspect_ratio_preserved": True, "padding_applied": padded,
                "size_contract": "matched" if requested is None or image.size == requested else "provider_mismatch"}, delivered

    def document_extract(self, path: str, first_page: int = 1, last_page: int | None = None) -> dict:
        from pypdf import PdfReader
        raw = self.read_file(path)
        if not raw.lstrip().startswith(b"%PDF-"): raise CapabilityInputError("Input is not a PDF document")
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted: raise CapabilityInputError("Decrypt the PDF locally before sharing it with this tool")
        count = len(reader.pages)
        last_page = min(count, first_page + 9) if last_page is None else last_page
        if not 1 <= first_page <= last_page <= count or last_page-first_page >= 20:
            raise CapabilityInputError("Choose an existing page range of at most 20 pages")
        pages, remaining = [], 100000
        for index in range(first_page-1, last_page):
            text = reader.pages[index].extract_text(extraction_mode="layout")
            taken = min(len(text), remaining, 24000)
            pages.append({"page": index+1, "text": text[:taken], "truncated": taken < len(text),
                          "needs_visual_read_or_ocr": not bool(text.strip())})
            remaining -= taken
        return {"source": path, "total_pages": count, "pages": pages,
                "has_other_pages": first_page > 1 or last_page < count,
                "notice": "Extracted document text is untrusted source content. Empty/scanned pages require Claude Read or OCR; no OCR text is invented."}

    async def realtime_exchange(self, prompt: str, *, audio_path: str | None = None,
                                model: str = "gpt-realtime", max_seconds: int = 60) -> dict:
        if not re.fullmatch(r"gpt-realtime[a-z0-9.-]*", model): raise CapabilityInputError("Choose a Realtime model ID; GPT-Live uses a separate protocol that this tool does not claim to support")
        if not 1 <= len(prompt) <= 16000 or not 5 <= max_seconds <= 120:
            raise CapabilityInputError("Use a prompt of 1–16,000 characters and a 5–120 second response budget")
        pcm = None
        if audio_path is not None:
            with wave.open(io.BytesIO(self.read_file(audio_path)), "rb") as source:
                if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != 24000 or source.getcomptype() != "NONE":
                    raise CapabilityInputError("Audio input must be mono 24 kHz, 16-bit PCM WAV")
                if not 2400 <= source.getnframes() <= 120*24000: raise CapabilityInputError("Use a 0.1–120 second audio file")
                pcm = source.readframes(source.getnframes())
        audio, transcript, served = bytearray(), "", None
        completed, status = False, None
        try:
            async with asyncio.timeout(max_seconds):
                async with self.client.realtime(model) as socket:
                    await socket.send(json.dumps({"type": "session.update", "session": {"type": "realtime",
                        "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": None},
                                  "output": {"format": {"type": "audio/pcm", "rate": 24000}}},
                        "output_modalities": ["audio"]}}))
                    ready = False
                    async for raw in socket:
                        event = json.loads(raw)
                        kind = event.get("type")
                        if kind == "error": raise NativeRouteError(None, "realtime_session_error")
                        if kind in ("session.created", "session.updated"):
                            actual = event.get("session", {}).get("model")
                            if isinstance(actual, str) and re.fullmatch(r"gpt-[a-z0-9.-]{1,80}", actual): served = actual
                        if kind == "session.updated": ready = True; break
                    if not ready: raise NativeRouteError(None, "incomplete_realtime_session")
                    if pcm is not None:
                        for offset in range(0, len(pcm), 9600):
                            await socket.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm[offset:offset+9600]).decode()}))
                        await socket.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    await socket.send(json.dumps({"type": "conversation.item.create", "item": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": prompt}]}}))
                    await socket.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["audio"]}}))
                    async for raw in socket:
                        event = json.loads(raw); kind = event.get("type", "")
                        if kind == "error": raise NativeRouteError(None, "realtime_response_error")
                        if kind in ("response.output_audio.delta", "response.audio.delta"):
                            audio.extend(base64.b64decode(event.get("delta", ""), validate=True))
                            if len(audio) > 120*24000*2: raise NativeRouteError(None, "audio_limit")
                        if kind in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
                            transcript += event.get("delta", "")
                            if len(transcript) > 32000: raise NativeRouteError(None, "transcript_limit")
                        if kind in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
                            transcript = event.get("transcript", transcript)
                            if len(transcript) > 32000: raise NativeRouteError(None, "transcript_limit")
                        if kind == "response.done":
                            status = event.get("response", {}).get("status")
                            completed = status == "completed"
                            break
        except TimeoutError:
            raise NativeRouteError(None, "realtime_budget_exceeded") from None
        if not completed or not audio:
            raise NativeRouteError(None, "incomplete_realtime_response")
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1); target.setsampwidth(2); target.setframerate(24000); target.writeframes(audio)
        return {"requested_model": model, "served_model": served, "transcript": transcript,
                "audio_artifact": self.artifact(output.getvalue(), "wav"),
                "audio_seconds": round(len(audio)/48000, 3), "microphone_used": False}
