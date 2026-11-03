"""Atomic snapshot experiments use temporary synthetic roots only."""

import hashlib
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import quota_snapshot_fixture as snapshot
import runtime_bundle


SOURCE = Path(__file__).resolve().parent.parent


@unittest.skipUnless(sys.platform == "darwin", "Requires Darwin exclusive rename")
class SnapshotFixtureTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="quota-snapshot-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.addCleanup(self.restore_directories)
        runtime_bundle.write_file(self.root / ".quota-handoff-fixture", b"synthetic quota fixture\n", 0o600)
        self.reviewed = self.root / "reviewed"
        self.reviewed.mkdir(mode=0o700)
        for name in snapshot.FILES:
            shutil.copyfile(SOURCE / name, self.reviewed / name)
        self.expected = {name: hashlib.sha256((self.reviewed / name).read_bytes()).hexdigest() for name in snapshot.FILES}

    def restore_directories(self):
        for path in self.root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)

    def test_publishes_and_reuses_only_manifest_matched_sources(self):
        result = snapshot.publish_fixture(self.root, self.reviewed, self.expected)
        snapshot.verify_fixture(self.root, result, self.expected)
        self.assertEqual(result, snapshot.publish_fixture(self.root, self.reviewed, self.expected))
        self.assertEqual({path.name for path in result.iterdir()}, set(snapshot.FILES) | {"manifest.json"})
        self.assertEqual(result.stat().st_mode & 0o777, 0o500)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o400 for path in result.iterdir()))
        self.assertFalse((self.root / "auth").exists())

    def test_native_read_only_stage_is_published_or_refused_without_overwrite(self):
        runtime = self.root / "runtime-quota"
        runtime_bundle.private_dir(runtime)
        parent = runtime / "source-snapshots"
        runtime_bundle.private_dir(parent)
        content, digest = snapshot._manifest(self.expected)
        stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=parent))
        for name in snapshot.FILES:
            runtime_bundle.write_file(stage / name, (self.reviewed / name).read_bytes(), 0o400)
        runtime_bundle.write_file(stage / "manifest.json", content, 0o400)
        stage.chmod(0o500)
        try:
            snapshot._publish_exclusive(stage, parent / digest)
        except PermissionError as error:
            # macOS 15 can reject renaming a directory without owner write access.
            self.assertEqual(error.errno, errno.EACCES)
            self.assertTrue(stage.is_dir())
            self.assertFalse((parent / digest).exists())
            return
        except (OSError, AttributeError) as error:
            self.fail(f"Synthetic exclusive publication failed: {type(error).__name__}, errno={getattr(error, 'errno', None)}")
        snapshot.verify_fixture(self.root, parent / digest, self.expected)

    def test_native_publication_with_a_writable_source_directory_control(self):
        runtime = self.root / "runtime-quota"
        runtime_bundle.private_dir(runtime)
        parent = runtime / "source-snapshots"
        runtime_bundle.private_dir(parent)
        stage = Path(tempfile.mkdtemp(prefix=".staging-control-", dir=parent))
        runtime_bundle.write_file(stage / "synthetic", b"fixture", 0o400)
        try:
            snapshot._publish_exclusive(stage, parent / "control")
        except (OSError, AttributeError) as error:
            self.fail(f"Synthetic writable publication failed: {type(error).__name__}, errno={getattr(error, 'errno', None)}")
        self.assertTrue((parent / "control" / "synthetic").is_file())

    def test_manifest_mismatch_is_refused_before_publication(self):
        (self.reviewed / "quota_recovery.py").write_text("# synthetic change\n", encoding="utf-8")
        with self.assertRaises(snapshot.SnapshotUnavailable):
            snapshot.publish_fixture(self.root, self.reviewed, self.expected)
        self.assertFalse((self.root / "runtime-quota").exists())

    def test_invalid_manifest_and_fixture_auth_are_refused_before_reading_sources(self):
        with patch.object(snapshot, "_read") as read:
            with self.assertRaises(snapshot.SnapshotUnavailable):
                snapshot.publish_fixture(self.root, self.reviewed, {})
        read.assert_not_called()
        (self.root / "auth").mkdir(mode=0o700)
        with patch.object(snapshot, "_read") as read:
            with self.assertRaises(snapshot.SnapshotUnavailable):
                snapshot.publish_fixture(self.root, self.reviewed, self.expected)
        read.assert_not_called()
        self.assertFalse((self.root / "runtime-quota").exists())

    def test_symlinked_source_is_refused_before_publication(self):
        path = self.reviewed / "quota_recovery.py"
        path.unlink()
        path.symlink_to(SOURCE / "quota_recovery.py")
        with self.assertRaises(snapshot.SnapshotUnavailable):
            snapshot.publish_fixture(self.root, self.reviewed, self.expected)
        self.assertFalse((self.root / "runtime-quota").exists())

    def test_tampered_published_snapshot_is_not_reused(self):
        result = snapshot.publish_fixture(self.root, self.reviewed, self.expected)
        result.chmod(0o700)
        path = result / "quota_recovery.py"
        path.chmod(0o600)
        path.write_text("# synthetic change\n", encoding="utf-8")
        path.chmod(0o400)
        result.chmod(0o500)
        with self.assertRaises(snapshot.SnapshotUnavailable):
            snapshot.publish_fixture(self.root, self.reviewed, self.expected)

    def test_extra_snapshot_file_is_not_reused_or_deleted(self):
        result = snapshot.publish_fixture(self.root, self.reviewed, self.expected)
        result.chmod(0o700)
        extra = result / "synthetic-extra"
        extra.write_bytes(b"fixture")
        result.chmod(0o500)
        with self.assertRaises(snapshot.SnapshotUnavailable):
            snapshot.verify_fixture(self.root, result, self.expected)
        self.assertEqual(extra.read_bytes(), b"fixture")

    def test_failed_stage_is_removed_without_publishing(self):
        real_write = runtime_bundle.write_file

        def fail_one(path, content, mode):
            if path.name == "api_profile.py":
                raise OSError("synthetic write failure")
            return real_write(path, content, mode)

        with patch.object(runtime_bundle, "write_file", side_effect=fail_one):
            with self.assertRaises(snapshot.SnapshotUnavailable):
                snapshot.publish_fixture(self.root, self.reviewed, self.expected)
        parent = self.root / "runtime-quota" / "source-snapshots"
        self.assertEqual(list(parent.iterdir()), [])

    def test_competing_empty_destination_is_not_overwritten(self):
        publish = snapshot._publish_exclusive
        seen = []

        def conflict(stage, final):
            final.mkdir(mode=0o700)
            final.chmod(0o500)
            seen.append(final)
            return publish(stage, final)

        with patch.object(snapshot, "_publish_exclusive", side_effect=conflict):
            with self.assertRaises(snapshot.SnapshotUnavailable):
                snapshot.publish_fixture(self.root, self.reviewed, self.expected)
        self.assertEqual(len(seen), 1)
        self.assertEqual(list(seen[0].iterdir()), [])
        self.assertEqual({path.name for path in seen[0].parent.iterdir()}, {seen[0].name})

    def test_unsupported_exclusive_rename_never_falls_back_to_overwrite(self):
        with patch.object(snapshot, "_publish_exclusive", side_effect=OSError(errno.ENOTSUP, "synthetic")):
            with self.assertRaises(snapshot.SnapshotUnavailable):
                snapshot.publish_fixture(self.root, self.reviewed, self.expected)
        self.assertEqual(list((self.root / "runtime-quota" / "source-snapshots").iterdir()), [])

    def test_crash_during_staging_leaves_no_published_snapshot_and_retry_works(self):
        program = r'''
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import quota_snapshot_fixture,runtime_bundle
write=runtime_bundle.write_file
def crash(path,content,mode):
    if path.name=='api_profile.py':
        os._exit(47)
    return write(path,content,mode)
runtime_bundle.write_file=crash
quota_snapshot_fixture.publish_fixture(Path(sys.argv[2]),Path(sys.argv[3]),json.loads(sys.argv[4]))
raise AssertionError('Synthetic crash did not occur')
'''
        environment = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ}
        environment["HOME"] = str(self.root)
        result = subprocess.run([sys.executable, "-I", "-c", program, str(SOURCE), str(self.root),
                                 str(self.reviewed), json.dumps(self.expected)], cwd=self.root, env=environment,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (47, "", ""))
        parent = self.root / "runtime-quota" / "source-snapshots"
        leftover = list(parent.iterdir())
        self.assertEqual(len(leftover), 1)
        self.assertTrue(leftover[0].name.startswith(".staging-"))
        published = snapshot.publish_fixture(self.root, self.reviewed, self.expected)
        snapshot.verify_fixture(self.root, published, self.expected)
        self.assertTrue(leftover[0].is_dir())  # Unknown crash remnants are not silently deleted.

    def test_crash_after_rename_leaves_a_verifiable_snapshot(self):
        program = r'''
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import quota_snapshot_fixture
quota_snapshot_fixture.verify_fixture=lambda *_args:os._exit(48)
quota_snapshot_fixture.publish_fixture(Path(sys.argv[2]),Path(sys.argv[3]),json.loads(sys.argv[4]))
raise AssertionError('Synthetic crash did not occur')
'''
        environment = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ}
        environment["HOME"] = str(self.root)
        result = subprocess.run([sys.executable, "-I", "-c", program, str(SOURCE), str(self.root),
                                 str(self.reviewed), json.dumps(self.expected)], cwd=self.root, env=environment,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (48, "", ""))
        parent = self.root / "runtime-quota" / "source-snapshots"
        entries = list(parent.iterdir())
        self.assertEqual(len(entries), 1)
        snapshot.verify_fixture(self.root, entries[0], self.expected)
        self.assertEqual(snapshot.publish_fixture(self.root, self.reviewed, self.expected), entries[0])

    def test_crash_between_rename_and_seal_leaves_unusable_snapshot(self):
        program = r'''
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import quota_snapshot_fixture
publish=quota_snapshot_fixture._publish_exclusive
def crash(stage,final):
    publish(stage,final)
    os._exit(49)
quota_snapshot_fixture._publish_exclusive=crash
quota_snapshot_fixture.publish_fixture(Path(sys.argv[2]),Path(sys.argv[3]),json.loads(sys.argv[4]))
raise AssertionError('Synthetic crash did not occur')
'''
        environment = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ}
        environment["HOME"] = str(self.root)
        result = subprocess.run([sys.executable, "-I", "-c", program, str(SOURCE), str(self.root),
                                 str(self.reviewed), json.dumps(self.expected)], cwd=self.root, env=environment,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (49, "", ""))
        parent = self.root / "runtime-quota" / "source-snapshots"
        entries = list(parent.iterdir())
        self.assertEqual(len(entries), 1)
        final = entries[0]
        self.assertEqual(final.stat().st_mode & 0o777, 0o700)
        original = {path.name: path.read_bytes() for path in final.iterdir()}
        with self.assertRaises(snapshot.SnapshotUnavailable):
            snapshot.verify_fixture(self.root, final, self.expected)
        with self.assertRaises(snapshot.SnapshotUnavailable):
            snapshot.publish_fixture(self.root, self.reviewed, self.expected)
        self.assertEqual(final.stat().st_mode & 0o777, 0o700)
        self.assertEqual({path.name: path.read_bytes() for path in final.iterdir()}, original)

    def test_seal_rejects_a_replacement_inode_without_changing_permissions(self):
        stage = self.root / "stage"
        final = self.root / "final"
        stage.mkdir(mode=0o700)
        final.mkdir(mode=0o700)
        info = stage.stat()
        with self.assertRaises(snapshot.SnapshotUnavailable):
            snapshot._seal_published(final, info.st_dev, info.st_ino)
        self.assertEqual(final.stat().st_mode & 0o777, 0o700)

    def test_independent_publishers_converge_without_overwriting(self):
        program = r'''
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import quota_snapshot_fixture
root,source=Path(sys.argv[2]),Path(sys.argv[3])
result=quota_snapshot_fixture.publish_fixture(root,source,json.loads(sys.argv[4]))
print(result.name)
'''
        environment = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ}
        environment["HOME"] = str(self.root)
        children = [subprocess.Popen([sys.executable, "-I", "-c", program, str(SOURCE), str(self.root),
                                      str(self.reviewed), json.dumps(self.expected)], cwd=self.root, env=environment,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True) for _ in range(2)]
        outputs = []
        try:
            for child in children:
                out, err = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, "Synthetic snapshot process failed")
                self.assertEqual(err, "", "Synthetic snapshot process wrote to stderr")
                outputs.append(out.strip())
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.communicate(timeout=5)
                if child.stdout and not child.stdout.closed:
                    child.stdout.close()
                if child.stderr and not child.stderr.closed:
                    child.stderr.close()
        self.assertEqual(len(set(outputs)), 1)
        result = self.root / "runtime-quota" / "source-snapshots" / outputs[0]
        snapshot.verify_fixture(self.root, result, self.expected)
        self.assertEqual({path.name for path in result.parent.iterdir()}, {result.name})


@unittest.skipIf(sys.platform == "darwin", "Exercises the unsupported-platform refusal")
class UnsupportedSnapshotFixtureTest(unittest.TestCase):
    def test_refuses_without_creating_state(self):
        with tempfile.TemporaryDirectory(prefix="quota-snapshot-unsupported-") as directory:
            root = Path(directory)
            with self.assertRaises(snapshot.SnapshotUnavailable):
                snapshot.publish_fixture(root, root, {})
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
