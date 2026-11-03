"""Exercise a disposable launchd job, never the installed bridge or its account.

Explicit local opt-in is required. The temporary job uses the production
autostart entry with a fake verifier that always fails and never starts a
service. This is not an actual logout, reboot, or model inference test.
"""

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import autostart  # noqa: E402


SOURCE = Path(__file__).resolve().parents[1]
MODULES = ("autostart.py", "launcher.py", "model_helper.py", "rolling_runtime.py", "runtime_bundle.py", "api_profile.py")
PREFIX = "org.claude-codex-http-bridge.fixture."
SENTINEL_NAME = "CLAUDE_CODEX_FIXTURE_SHELL_ONLY"


def definition(root, python, label, *, base_definition=autostart.agent_definition):
    if not re.fullmatch(re.escape(PREFIX) + r"[a-f0-9]{32}", label):
        raise ValueError("Invalid disposable job label")
    value = plistlib.loads(base_definition(root, python))
    value["Label"] = label
    # A shorter interval makes this fixture bounded. An inert proxy environment
    # skips reading the operator's system proxy; the fake helper never connects.
    value["StartInterval"] = 10
    value["EnvironmentVariables"]["HOME"] = str(root / "home")
    value["EnvironmentVariables"]["HTTP_PROXY"] = "http://127.0.0.1:9"
    return plistlib.dumps(value, sort_keys=True)


def launchctl(*arguments):
    return subprocess.run(["/bin/launchctl", *arguments], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)


def wait_status(path, expected, *, after_ns=0, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            stamp = path.stat().st_mtime_ns
            value = json.loads(path.read_text(encoding="utf-8"))
            if stamp > after_ns and value.get("result") == expected:
                return stamp
        except (OSError, ValueError, AttributeError):
            pass
        time.sleep(.1)
    raise RuntimeError("Disposable launchd state did not reach the expected result")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-local-launchd", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin" or not args.run_local_launchd or os.environ.get("GITHUB_ACTIONS") == "true":
        raise RuntimeError("This fixture requires explicit opt-in on a local macOS login")
    root = Path(tempfile.mkdtemp(prefix="bridge-launchd-fixture-")).resolve()
    label = PREFIX + secrets.token_hex(16)
    domain = f"gui/{os.getuid()}"
    target = domain + "/" + label
    bootstrap_attempted = False
    report = None
    previous = os.environ.get(SENTINEL_NAME)
    os.environ[SENTINEL_NAME] = "synthetic-shell-value"
    try:
        (root / "home").mkdir(mode=0o700)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        for name in MODULES:
            shutil.copyfile(SOURCE / name, root / name)
        # This helper only emits fixed booleans to the owned fixture, and fails
        # before the real launcher can start or contact any service.
        helper = root / "bridge_models.py"
        helper.write_text(
            "from pathlib import Path\nimport json,os,sys\n"
            "root=Path(__file__).resolve().parent\n"
            "try: policy=json.loads((root/'runtime'/'autostart-network.json').read_text())\n"
            "except (OSError,ValueError): policy={}\n"
            "result={'verify_only':sys.argv[1:]==['verify'],"
            "'fixture_home':Path.home().resolve()==(root/'home').resolve(),"
            "'shell_sentinel_absent':'CLAUDE_CODEX_FIXTURE_SHELL_ONLY' not in os.environ,"
            "'expected_policy':policy.get('bypass')=='expected.fixture'}\n"
            "(root/'verify-result.json').write_text(json.dumps(result))\n"
            "raise SystemExit(1)\n", encoding="utf-8")
        (runtime / "autostart-paused").write_text("paused\n", encoding="utf-8")
        job = root / "fixture.plist"
        job.write_bytes(definition(root, sys.executable, label))
        job.chmod(0o600)
        if launchctl("print", target).returncode == 0:
            raise RuntimeError("Disposable launchd label is already in use")
        bootstrap_attempted = True
        if launchctl("bootstrap", domain, str(job)).returncode != 0:
            raise RuntimeError("Could not bootstrap the disposable launchd job")
        status = runtime / "autostart-status.json"
        first = wait_status(status, "paused")
        second = wait_status(status, "paused", after_ns=first, timeout=25)
        (runtime / "autostart-paused").unlink()
        if launchctl("kickstart", target).returncode != 0:
            raise RuntimeError("Could not trigger the disposable launchd job")
        failed = wait_status(status, "failed", after_ns=second)
        verify = json.loads((root / "verify-result.json").read_text(encoding="utf-8"))
        if verify != {"verify_only": True, "fixture_home": True, "shell_sentinel_absent": True,
                      "expected_policy": False}:
            raise RuntimeError("Disposable verifier isolation did not hold")
        (runtime / "autostart-paused").write_text("paused\n", encoding="utf-8")
        if launchctl("kickstart", target).returncode != 0:
            raise RuntimeError("Could not retrigger the disposable launchd job")
        wait_status(status, "paused", after_ns=failed)

        # Exercise the real enable path with a fresh RunAtLoad job. Delay the
        # bootstrap return so that the child has a chance to run before the
        # parent continues; the child must see the newly published policy.
        registered = launchctl("print", target)
        if registered.returncode != 0 or os.fsencode(str(root / "autostart.py")) not in registered.stdout:
            raise RuntimeError("Disposable launchd identity changed before enable test")
        if launchctl("bootout", target).returncode != 0 or launchctl("print", target).returncode == 0:
            raise RuntimeError("Could not unload the first disposable job")
        (runtime / "autostart-paused").unlink()
        status.unlink()
        (root / "verify-result.json").unlink()
        old_policy = {"format": 1, "source": "system", "schemes": ["https"], "bypass": "old.fixture"}
        desired = {"format": 1, "source": "system", "schemes": ["https"], "bypass": "expected.fixture"}
        autostart.runtime_bundle.write_file(runtime / "autostart-network.json",
                                            autostart.runtime_bundle.canonical(old_policy), 0o600)
        job = root / "enable.plist"
        real_launchctl = autostart.launchctl

        def delayed_launchctl(*arguments):
            result = real_launchctl(*arguments)
            if arguments[0] == "bootstrap" and result.returncode == 0:
                time.sleep(1)
            return result

        def fixture_definition(fixture_root, python=None):
            return definition(fixture_root, python or sys.executable, label)

        with patch.object(autostart, "label", return_value=label), \
             patch.object(autostart, "agent_path", return_value=job), \
             patch.object(autostart, "agent_definition", side_effect=fixture_definition), \
             patch.object(autostart, "capture_macos_network_policy", return_value=desired), \
             patch.object(autostart, "launchctl", side_effect=delayed_launchctl):
            autostart.enable(root)
        wait_status(status, "failed")
        verify = json.loads((root / "verify-result.json").read_text(encoding="utf-8"))
        if verify != {"verify_only": True, "fixture_home": True, "shell_sentinel_absent": True,
                      "expected_policy": True}:
            raise RuntimeError("RunAtLoad did not observe the newly published policy")
        # The normal disable path owns the same operation lock. Make the
        # disposable job wait on that lock, then verify launchd can unload it.
        with patch.object(autostart, "label", return_value=label), \
             patch.object(autostart, "agent_path", return_value=job), \
             patch.object(autostart, "agent_definition", side_effect=fixture_definition):
            with autostart.operation_lock(root):
                if launchctl("kickstart", target).returncode != 0:
                    raise RuntimeError("Could not trigger the disposable lock-wait test")
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    state = launchctl("print", target)
                    if state.returncode == 0 and b"state = running" in state.stdout:
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError("Disposable job did not start the lock-wait test")
                time.sleep(.5)
                autostart.disable(root)
        if job.exists() or launchctl("print", target).returncode == 0:
            raise RuntimeError("Disposable job was not fully disabled")
        report = {"synthetic_only": True, "real_launchd_job": True, "actual_login_or_reboot": False,
            "initial_paused_run": True, "periodic_paused_run": True, "fake_verifier_failure_recorded": True,
            "pause_after_failure": True, "fixture_home_and_shell_isolation": True,
            "enable_run_at_load_observed_new_policy": True,
            "disable_while_job_waits_for_lock": True,
            "real_service_or_account_used": False}
    finally:
        if previous is None:
            os.environ.pop(SENTINEL_NAME, None)
        else:
            os.environ[SENTINEL_NAME] = previous
        if bootstrap_attempted:
            registered = launchctl("print", target)
            if registered.returncode == 0:
                if os.fsencode(str(root / "autostart.py")) not in registered.stdout:
                    raise RuntimeError("Disposable launchd identity changed; its private fixture was retained")
                launchctl("bootout", target)
                if launchctl("print", target).returncode == 0:
                    launchctl("bootout", domain, str(job))
                if launchctl("print", target).returncode == 0:
                    raise RuntimeError("Disposable launchd job could not be removed; its private fixture was retained")
            elif launchctl("print", domain).returncode != 0:
                raise RuntimeError("Disposable launchd cleanup could not be verified; its private fixture was retained")
        shutil.rmtree(root)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        raise SystemExit("Disposable launchd observation failed; raw output withheld") from None
