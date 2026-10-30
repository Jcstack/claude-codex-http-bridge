"""Reject unreviewed or modified runtime code before changing an active choice."""

import json
import os
from pathlib import Path
import tempfile
import unittest

import runtime_bundle


class BundleTests(unittest.TestCase):
    def fixture(self, root):
        proxy, ingress = root / "proxy-input", root / "ingress-input"
        proxy.write_bytes(b"synthetic-proxy-fixture"); ingress.write_bytes(b"synthetic-ingress-fixture")
        bundle = root / "bundle"
        runtime_bundle.pack(proxy, ingress, bundle)
        return bundle

    def test_modified_binary_and_source_are_rejected_before_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runtime_bundle.private_dir(root)
            bundle = self.fixture(root)
            destination = root / "install"; runtime_bundle.private_dir(destination)
            first = runtime_bundle.activate(destination, bundle)
            selected = (destination / "runtime-selected.json").read_bytes()
            value, identity = runtime_bundle.verify(bundle)
            self.assertEqual(first, identity)
            binary = bundle / value["binaries"]["proxy"]["file"]
            binary.write_bytes(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                runtime_bundle.activate(destination, bundle)
            self.assertEqual((destination / "runtime-selected.json").read_bytes(), selected)
            value["source"]["upstream"] = "0" * 40
            (bundle / "bundle.json").write_text(json.dumps(value))
            with self.assertRaisesRegex(RuntimeError, "source lock"):
                runtime_bundle.verify(bundle)

    def test_bundle_paths_cannot_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runtime_bundle.private_dir(root)
            bundle = self.fixture(root)
            value, _ = runtime_bundle.verify(bundle)
            value["binaries"]["proxy"]["file"] = "../credential-fixture"
            (bundle / "bundle.json").write_text(json.dumps(value))
            with self.assertRaisesRegex(RuntimeError, "path"):
                runtime_bundle.verify(bundle)


if __name__ == "__main__": unittest.main()
