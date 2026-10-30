"""Credential-free end-to-end lifecycle probe, including a persistent Claude.

Fake OpenAI origin -> guarded TLS ingress -> reviewed translator -> same ingress
-> Claude. Replaces only disposable backends, never any installed user service.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch

from live_compare_translators import stream
import api_profile
import launcher
import rolling_runtime
import runtime_bundle


MODEL = "gpt-6.1-sol"
SPEC = {"context_window": 872000, "input_modalities": ["text", "image"], "supports_search_tool": True,
        "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")]}


class Upstream(BaseHTTPRequestHandler):
    requests = []
    admitted = threading.Event()
    release = threading.Event()

    def log_message(self, *_args): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer " + "a" * 64:
            self.send_error(401); return
        index = len(self.requests); self.requests.append(body)
        output = {"type": "message", "role": "assistant", "id": "msg_fixture", "phase": "final_answer",
                  "content": [{"type": "output_text", "text": "FIXTURE", "annotations": []}]}
        events = [{"type": "response.created", "response": {"id": "resp_fixture", "model": MODEL}},
                  {"type": "response.output_item.done", "item": output},
                  {"type": "response.completed", "response": {"id": "resp_fixture", "model": MODEL,
                    "output": [output], "status": "completed", "usage": {"input_tokens": 1000, "output_tokens": 10}}}]
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for n, event in enumerate(events):
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode()); self.wfile.flush()
            if index == 0 and n == 0:
                self.admitted.set()
                if not self.release.wait(90): raise RuntimeError("Synthetic handoff barrier timed out")


def persistent_claude(root, value, base_runtime, base, *, background=False):
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude: raise RuntimeError("Claude fixture binary is required")
    config = root / "claude-config"; runtime_bundle.private_dir(config)
    work = root / "workspace"; work.mkdir()
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP") if k in os.environ}
    env.update({"CLAUDE_CONFIG_DIR": str(config), "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "CLAUDE_CODE_ATTRIBUTION_HEADER": "0", "DISABLE_TELEMETRY": "1"})
    env = rolling_runtime.client_environment(root, value, env, config)
    settings = config / "fixture-settings.json"
    api_profile.write_private(settings, json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
        "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}))
    settings = rolling_runtime.client_settings(settings, env, config)
    process = subprocess.Popen([claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--settings", str(settings), "--model", MODEL, "--tools", "", "--permission-mode", "dontAsk", "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose", "-p"],
        cwd=work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    events = queue.Queue()
    seen, failures = [], set()

    def reader():
        for line in process.stdout:
            try: events.put(json.loads(line))
            except ValueError: pass
        events.put({"type": "fixture_eof"})

    thread = threading.Thread(target=reader, daemon=True); thread.start()
    def errors():
        for line in process.stderr:
            for label in ("certificate", "tls", "ssl", "unauthorized", "auth", "error", "model"):
                if label in line.lower(): failures.add(label)
    stderr_thread = threading.Thread(target=errors, daemon=True); stderr_thread.start()

    def turn(text):
        process.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
        process.stdin.flush()
        while True:
            try: event = events.get(timeout=45)
            except queue.Empty:
                raise RuntimeError("Persistent Claude timed out; events=" + repr(seen[-12:]) + "; error classes=" + repr(sorted(failures)) + "; origin requests=" + str(len(Upstream.requests))) from None
            seen.append({"type": event.get("type"), "subtype": event.get("subtype"), "keys": list(event),
                         "error_classes": [label for label in ("certificate", "tls", "ssl", "401", "400", "502", "503", "compressed", "gzip", "error", "auth") if label in json.dumps(event).lower()]})
            if event.get("type") == "fixture_eof": raise RuntimeError("Persistent Claude exited early")
            if event.get("type") == "result":
                if event.get("is_error") or "FIXTURE" not in event.get("result", ""):
                    raise RuntimeError("Persistent Claude turn failed; raw output withheld")
                return

    try:
        turn("fixture-first")
        updated = rolling_runtime.ensure(root, base_runtime, base, {MODEL: SPEC}, force=True)
        assert updated["pid"] == value["pid"] and updated["port"] == value["port"]
        turn("fixture-second")
        assert "fixture-first" in json.dumps(Upstream.requests[-1]["input"])
        assert "fixture-second" in json.dumps(Upstream.requests[-1]["input"])
        if background: background_claude(claude, env, settings, work)
    finally:
        launcher.stop_process(process)
        thread.join(timeout=2)


def background_claude(claude, env, settings, work):
    # Officially CLAUDE_CONFIG_DIR creates a separate supervisor instance:
    # https://code.claude.com/docs/en/agent-view#where-state-is-stored
    config = Path(env["CLAUDE_CONFIG_DIR"])
    assert config.name == "claude-config" and config.parent.name.startswith("bridge-managed-probe-")
    # Bootstrap trust only for this empty, disposable test workspace. This is a
    # version-pinned client fixture, never production launcher behavior.
    profile = config / ".claude.json"
    data = json.loads(profile.read_text()) if profile.exists() else {}
    projects = data.setdefault("projects", {})
    for path in {str(work), str(work.resolve()), work.resolve().as_posix()}:
        projects.setdefault(path, {})["hasTrustDialogAccepted"] = True
    api_profile.write_private(profile, json.dumps(data))

    def run(arguments, environment):
        result = subprocess.run([claude, *arguments], cwd=work, env=environment,
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            detail = (result.stdout + result.stderr).replace(str(work.parent), "<fixture>")
            for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
                if environment.get(name): detail = detail.replace(environment[name], "<local-key>")
            raise RuntimeError("Isolated background fixture command failed: " + detail[-600:])
        return result.stdout

    def rows():
        values = json.loads(run(["agents", "--json", "--all"], env))
        if any(not isinstance(row.get("cwd"), str) or Path(row["cwd"]).resolve() != work.resolve() for row in values):
            raise RuntimeError("Isolated background inventory contains an unrelated workspace")
        return [row for row in values if row.get("kind") == "background"]

    bad = {**env, "ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_AUTH_TOKEN": "synthetic-invalid"}
    common = ["--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
              "--model", MODEL, "--tools", "", "--permission-mode", "dontAsk", "--bg"]
    baseline = rows()
    baseline_ids = {row["id"] for row in baseline}
    try:
        run([*common, "Reply only with the synthetic fixture value."], bad)
        initial = [row for row in rows() if row["id"] not in baseline_ids]
        if len(initial) != 1: raise RuntimeError("Could not identify isolated warm-supervisor fixture")
        first_id = initial[0]["id"]
        run(["stop", first_id], env)
        # Dispatch without shell network variables. The profile must reach a
        # supervisor that was started with the deliberately wrong gateway.
        profile_env = {k: v for k, v in env.items() if not k.startswith("ANTHROPIC_") and k not in ("NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED")}
        before = len(Upstream.requests)
        run([*common, "--settings", str(settings), "Reply only with the synthetic fixture value."], profile_env)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            current = [row for row in rows() if row["id"] != first_id and row["id"] not in baseline_ids]
            if len(current) == 1 and len(Upstream.requests) > before and (current[0].get("state") == "done" or current[0].get("status") == "idle"):
                sid = current[0].get("sessionId")
                if isinstance(sid, str):
                    for transcript in (config / "projects").glob("*/" + sid + ".jsonl"):
                        for line in transcript.read_text(encoding="utf-8").splitlines():
                            record = json.loads(line)
                            if record.get("type") == "assistant" and any(b.get("text") == "FIXTURE" for b in record.get("message", {}).get("content", []) if isinstance(b, dict)):
                                return
            if current and current[0].get("state") in ("failed", "stopped"):
                raise RuntimeError("Background TLS/profile continuation failed")
            time.sleep(.25)
        details = [{name: row.get(name) for name in ("state", "status", "waitingFor")} for row in current]
        labels = set()
        for row in current:
            output = run(["logs", row["id"]], env).lower()
            for label in ("certificate", "tls", "auth", "gateway", "login", "trust", "127.0.0.1:1", "503", "500", "fixture"):
                if label in output: labels.add(label)
        raise RuntimeError("Background fixture did not finish; states=" + repr(details) + "; classes=" + repr(sorted(labels)) + "; origin_delta=" + str(len(Upstream.requests)-before))
    finally:
        # Scoped exclusively to the temporary config, never the user's daemon.
        subprocess.run([claude, "daemon", "stop", "--any"], cwd=work, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path); parser.add_argument("--claude-client", action="store_true")
    parser.add_argument("--background-client", action="store_true")
    args = parser.parse_args()
    Upstream.requests = []; Upstream.admitted.clear(); Upstream.release.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-managed-probe-") as temporary:
            root = Path(temporary); runtime_bundle.private_dir(root)
            base_runtime = root / "runtime"; runtime_bundle.private_dir(base_runtime)
            source = base_runtime / "session-fixture"; runtime_bundle.private_dir(source)
            for name, text in (("config.yaml", "synthetic"), ("key", "a" * 64), ("proxy.log", "")):
                api_profile.write_private(source / name, text)
            base = {"pid": os.getpid(), "port": server.server_port, "session": source.name}
            with patch.object(launcher, "ROOT", root):
                runtime_bundle.activate(root, args.bundle.resolve())
                value = None
                try:
                    value = rolling_runtime.ensure(root, base_runtime, base, {MODEL: SPEC})
                    runtime = root / "runtime-managed"
                    # Existing Windows manifests may use CRLF. Rollback must
                    # restore their exact bytes without another translation.
                    manifest_path = runtime / "backend.json"
                    original_bytes = manifest_path.read_bytes()
                    runtime_bundle.write_file(manifest_path, original_bytes.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"), 0o600)
                    key = rolling_runtime.private_key(runtime / "front-key")
                    ca = runtime / "ca.pem"
                    request = {"model": MODEL, "max_tokens": 100, "messages": [{"role": "user", "content": "fixture"}]}
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pending = pool.submit(stream, value["port"], key, request, ca_file=ca)
                        if not Upstream.admitted.wait(10):
                            classifications = set()
                            for log in runtime.glob("g-*/proxy.log"):
                                text = log.read_text(errors="replace").lower()
                                for label in ("unknown authority", "certificate", "tls handshake", "connection refused", "unsupported protocol"):
                                    if label in text: classifications.add(label)
                            raise AssertionError("Origin never admitted request: " + ", ".join(sorted(classifications)))
                        try: rolling_runtime.stop(root)
                        except RuntimeError as error: assert "active" in str(error)
                        else: raise AssertionError("Stopped an active stream")
                        previous = (runtime / "backend.json").read_bytes()
                        before_state = rolling_runtime.state(root)
                        with patch.object(rolling_runtime, "start", side_effect=RuntimeError("Synthetic replacement startup failure")):
                            try: rolling_runtime.ensure(root, base_runtime, base, {MODEL: SPEC}, force=True)
                            except RuntimeError as error: assert "Synthetic" in str(error)
                            else: raise AssertionError("Replacement failure was hidden")
                        assert (runtime / "backend.json").read_bytes() == previous
                        assert rolling_runtime.state(root) == before_state
                        updated = rolling_runtime.ensure(root, base_runtime, base, {MODEL: SPEC}, force=True)
                        assert updated["pid"] == value["pid"] and updated["port"] == value["port"]
                        assert len(updated["generations"]) == 2
                        content, _ = stream(value["port"], key, request, ca_file=ca)
                        assert "FIXTURE" == "".join(b.get("text", "") for b in content)
                        Upstream.release.set(); pending.result(timeout=20)
                    updated = rolling_runtime.ensure(root, base_runtime, base, {MODEL: SPEC})
                    assert len(updated["generations"]) == 1
                    assert launcher.listener_pids(base["port"]) == {base["pid"]}
                    for config in runtime.glob("g-*/config.yaml"):
                        assert "a" * 64 not in config.read_text(), "Legacy key reached translator config"
                    # A wrong legacy PID must fail before exposing its key.
                    origin = rolling_runtime.read(runtime / "origin.json")
                    wrong = {**origin, "pid": origin["pid"] + 1000000, "generation": "origin-wrong-fixture"}
                    rolling_runtime.write(runtime / "origin.json", wrong)
                    count = len(Upstream.requests)
                    try: stream(value["port"], key, request, ca_file=ca)
                    except RuntimeError: pass
                    else: raise AssertionError("Wrong legacy owner was accepted")
                    assert len(Upstream.requests) == count
                    rolling_runtime.write(runtime / "origin.json", origin)
                    stream(value["port"], key, request, ca_file=ca)
                    if args.claude_client: persistent_claude(root, updated, base_runtime, base, background=args.background_client)
                    print(json.dumps({"mock_only": True, "stable_tls_endpoint": "pass",
                        "old_stream_drained": "pass", "active_stop_refused": "pass", "legacy_service_preserved": "pass",
                        "legacy_key_not_given_to_translator": "pass", "origin_pid_checked_before_credentials": "pass",
                        "same_claude_process_after_backend_reload": "pass" if args.claude_client else "not-run",
                        "background_tls_with_warm_wrong_supervisor": "pass" if args.background_client else "not-run"}))
                finally:
                    Upstream.release.set()
                    if rolling_runtime.state(root) is not None: rolling_runtime.stop(root)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    main()
