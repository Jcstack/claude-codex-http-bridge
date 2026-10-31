"""Synthetic Claude Code compaction/continuation check on the subscription route.

Consumes quota. Uses a disposable configuration and never resumes a user session.
Reports metadata and exact fixture-state retention, not prompts or transcripts.
The modest fixture is not a substitute for a 367k-token production conversation.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

from live_support import existing_subscription, temporary_translator
import bridge_models


INITIAL = {"output_dir": "build/amber", "format": "jsonl", "retry_budget": "2",
           "offline_only": "true", "delete_source": "never", "min_version": "3.11",
           "compression": "gzip", "mode": "dry-run", "schema": "v3", "cache_ttl": "120",
           "unresolved_issue": "unicode-boundary", "next_action": "add-regression-test"}
UPDATES = {"output_dir": "build/violet", "retry_budget": "4", "schema": "v4", "cache_ttl": "90"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path); parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--efforts", nargs="+", choices=("high", "max"), default=["max", "high"])
    parser.add_argument("--native", action="store_true", help="Test the opt-in native V2 compact adapter")
    parser.add_argument("--model", default="sol")
    parser.add_argument("--recall-model", help="Optionally switch models after each compaction")
    parser.add_argument("--samples", type=int, default=600)
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.samples <= 18000 or not 1 <= args.repeat <= 3:
        raise ValueError("Use 1–18000 synthetic samples and 1–3 compact/recall rounds")
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("Claude Code is required")
    upstream = existing_subscription(args.root)
    aliases = bridge_models.aliases(upstream["models"])
    model = aliases.get(args.model, args.model)
    recall_model = aliases.get(args.recall_model, args.recall_model) if args.recall_model else model
    if any(value not in upstream["models"] or not bridge_models.eligible(upstream["models"][value]) for value in (model, recall_model)):
        raise RuntimeError("Requested models are not eligible in the authenticated catalog")
    observations = "\n".join(f"Historical sample {i:04d}: parser fixture {i % 37} completed; latency bucket {i % 13}; no configuration change."
                             for i in range(args.samples))
    prompt = ("We are maintaining a synthetic offline data-format tool. Remember this authoritative configuration: "
              + json.dumps(INITIAL) + "\nThe following historical observations do not change it.\n" + observations +
              "\nDo not perform actions. Acknowledge receipt with READY only.")
    expected = {**INITIAL, **UPDATES}
    failed = False
    catalog = {value: upstream["models"][value] for value in (model, recall_model)}
    with temporary_translator(args.binary.resolve(), upstream, model, models=catalog) as (port, key):
        for effort in args.efforts:
            with tempfile.TemporaryDirectory(prefix="bridge-compact-state-") as temporary:
                root = Path(temporary); work = root / "work"; work.mkdir()
                cfg = root / "claude-config"; cfg.mkdir(mode=0o700)
                settings = root / "settings.json"
                settings.write_text(json.dumps({"modelPicker": {"options": [{"model": value,
                    "label": "Compaction fixture", "behavesAs": "claude-sonnet-5-5"} for value in catalog], "replaceBuiltInOptions": True}}))
                env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP") if k in os.environ}
                env.update({"CLAUDE_CONFIG_DIR": str(cfg), "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                            "ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0", "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
                            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"})
                if args.native:
                    env["ANTHROPIC_CUSTOM_HEADERS"] = "X-Codex-Bridge-Native-Compaction: 1"
                common = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                          "--settings", str(settings), "--effort", effort,
                          "--tools", "", "--permission-mode", "dontAsk", "--output-format", "json"]
                sid = str(uuid.uuid4()); stages = []
                prompts = [("seed", prompt),
                           ("update", "Replace only these configuration values: " + json.dumps(UPDATES) + ". Keep every other value. Reply UPDATED only."),
                          ]
                for round_number in range(1, args.repeat + 1):
                    prompts.extend([(f"compact-{round_number}", "/compact Preserve the exact latest configuration, explicit restrictions, unresolved issue and next action; omit the historical samples."),
                                    (f"recall-{round_number}", "Return only a JSON object with all twelve current configuration keys and their string values. No markdown.")])
                answer = {}
                all_recalls_passed = True
                for stage, text in prompts:
                    start = time.monotonic()
                    selected = recall_model if stage.startswith("recall-") else model
                    # stdin avoids Windows' command-line length limit for the fixture.
                    command = [*common, "--model", selected, "--session-id" if stage == "seed" else "--resume", sid, "-p"]
                    try:
                        process = subprocess.run(command, input=text, cwd=work, env=env, capture_output=True, text=True, timeout=240)
                    except subprocess.TimeoutExpired:
                        raise RuntimeError("Synthetic compaction stage exceeded 240 seconds; current user sessions were not touched") from None
                    if process.returncode:
                        raise RuntimeError("Synthetic compaction client failed; raw output withheld")
                    result = json.loads(process.stdout)
                    if result.get("is_error"):
                        raise RuntimeError("Synthetic compaction returned an error; raw output withheld")
                    metric = {"stage": stage, "seconds": round(time.monotonic() - start, 3)}
                    usage = result.get("usage", {})
                    metric["usage"] = {k: v for k, v in usage.items() if k in (
                        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens") and type(v) is int}
                    stages.append(metric)
                    print(json.dumps({"model": selected, "effort": effort, **metric}), flush=True)
                    if stage.startswith("recall-"):
                        try:
                            answer = json.loads(result.get("result", ""))
                        except ValueError:
                            answer = {}
                        all_recalls_passed &= isinstance(answer, dict) and all(answer.get(k) == v for k, v in expected.items())
                compactions = []
                native_carrier = False
                for path in (cfg / "projects").glob(f"*/{sid}.jsonl"):
                    for line in path.open(encoding="utf-8"):
                        native_carrier |= "codex-native-context-v1:" in line
                        record = json.loads(line)
                        if record.get("subtype") == "compact_boundary":
                            meta = record.get("compactMetadata", {})
                            compactions.append({k: meta.get(k) for k in ("trigger", "preTokens", "postTokens", "durationMs")})
                correct = sum(isinstance(answer, dict) and answer.get(k) == v for k, v in expected.items())
                passed = all_recalls_passed and correct == len(expected) and len(compactions) == args.repeat and (not args.native or native_carrier)
                failed |= not passed
                print(json.dumps({"model": model, "effort": effort, "state_fields_correct": correct,
                                  "state_fields_total": len(expected), "compaction": compactions,
                                  "native_carrier": native_carrier, "pass": passed}), flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
