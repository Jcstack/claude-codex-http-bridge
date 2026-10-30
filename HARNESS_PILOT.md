# Local GPT harness pilot

Run on 2026-10-30. **Both paths solved this one synthetic task in both retained runs. Claude Code did not demonstrate a quality advantage; its observed runs took longer.** This is a transport and evaluation-method pilot, not a coding leaderboard or an Opus parity result. No Claude account or OpenAI project API key was available or used. Inference used the existing ChatGPT subscription.

## Task and setup

The task fixes a deliberately broken, dependency-free Python incremental SSE decoder. It requires arbitrary byte boundaries, UTF-8, CR/LF/CRLF, multiline and empty data, IDs, retry values, comments, BOM handling, and close behavior. The original implementation passes two visible tests and fails all seven external acceptance tests. Each agent received the same README, prompt and initial files in a fresh disposable Git checkout. Existing tests could not be changed; agents could add their own. No user project was used.

- Requested model: `gpt-6.1-sol`; requested effort: `high`; process timeout: 300 seconds per run.
- Native harness: Codex CLI `0.166.0`, ephemeral session, user config and rules disabled, `project_doc_max_bytes=0`, workspace-write sandbox, no approval escalation.
- Claude harness: Claude Code `2.1.314`, fresh private configuration, restricted mode, no MCP servers or plugins, only Bash/Read/Edit/Write, strict Bash sandbox with no network access or unsandboxed fallback. The model's inference connection remains available outside the tool sandbox.
- Claude transport: an isolated candidate built from CLIProxyAPI v8.0.46 source plus the two patches at bridge commit [`383f3f2`](https://github.com/Jcstack/claude-codex-http-bridge/commit/383f3f2). It forwards Responses to the existing authenticated localhost subscription service. The candidate has only a localhost client key; it never receives OAuth files or a project API key. Its extra localhost hop is part of this measurement.
- Native model identity is **requested, not independently confirmed**: `codex exec --json` did not emit a served-model field. Claude's translated assistant messages report `gpt-6.1-sol`; that field is not independent backend attestation either. Do not call this a fully verified same-served-model comparison.

The first two native runs also passed, but were excluded because `--ignore-user-config` alone did not establish that personal `AGENTS.md` instructions were absent. The two retained native runs explicitly set `project_doc_max_bytes=0`. They ran after the Claude runs, so the corrected comparison is **not counterbalanced** and can be affected by service load or warm caches. The harnesses' tool schemas, system instructions, permission implementations and token accounting also differ. Matching the effort label does not establish equal inference compute.

## Observations

Each retained run passed the same **7/7 external acceptance tests** and left the two original tests unchanged. Agent-added tests also passed. Tool counts include completed command/file-change items in Codex and `tool_use` blocks in Claude Code; these are similar activity measures, not identical work units.

| Path | Run | External tests | Wall time | Tool activity | Total input tokens | Cached input subset | Output tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Native Codex | 1 | 7/7 | 95.79 s | 7 | 138,122 | 124,032 | 3,500 |
| Native Codex | 2 | 7/7 | 78.10 s | 7 | 131,041 | 118,784 | 3,998 |
| Claude Code + candidate | 1 | 7/7 | 275.32 s | 15 | 137,264 | 121,088 | 6,582 |
| Claude Code + candidate | 2 | 7/7 | 232.49 s | 13 | 141,915 | 104,704 | 5,296 |

Codex reports input tokens inclusive of cached input. Claude's total above is `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`; cache creation was zero. These are cumulative reported usage counters, not the maximum context occupied, monetary costs or subscription quota percentages. Output counters retain each harness's own reporting convention; reasoning tokens are not added again.

The four processes exited successfully. Recorded traces contain no acceptance-directory path or permission denial. This is a trace check, not proof of an OS-enforced read barrier for every native tool. Acceptance tests ran only after each model process exited. A source review found ordinary incremental UTF-8/line-state implementations without fixture-specific output; it was not a blinded independent review. Passing seven cases does not prove full SSE conformance.

The retained native runs averaged **86.95 seconds** and the Claude runs **253.91 seconds**. This small, sequential sample shows a local performance difference worth investigating, not a causal or general harness speed ranking. It does not test MCP discovery, subagents, web search, images, long-context continuation, computer use, live media, or Windows inference. Those capabilities need separate scenarios. The broader [model and harness evidence](MODEL_HARNESS_EVIDENCE.md) remains the basis for assessing the Opus target.

## Reproduce the fixture safely

The [preparation script](tests/prepare_harness_pilot.py) contains only synthetic task material and performs no network or model calls. It creates separate `workspace/`, `acceptance/`, `prompt.txt`, and `manifest.json` entries under a new directory. UTF-8 and LF are explicit for Windows reproducibility. It refuses to overwrite an existing destination.

```sh
python3 tests/prepare_harness_pilot.py /path/to/new-pilot
```

Give the evaluated agent only a fresh copy of `workspace/` and the text in `prompt.txt`. Keep acceptance tests outside its readable roots; separate directories alone are not an isolation boundary. Do not point it at this bridge repository, which contains the test generator. Run the model with its own appropriate sandbox, private account configuration, no project API route, no network tools, and a fixed timeout. Use the documented harness versions and flags above as the configuration record, not a claim that their policies are equivalent.

After the agent has exited, run these from its workspace, substituting the acceptance directory's absolute path:

```sh
python3 -m unittest discover -s tests
python3 -m unittest discover -s /path/to/new-pilot/acceptance
```

The original `sse_decoder.py` SHA-256 is `e43f399d5aa0bdccf5852a05851187bcf3d07eefe0e08cbb047240aa025845a8`. The script emits per-file hashes. The macOS candidate used for the live pilot had SHA-256 `c70cb698e24546c39417b4f05ad51e6d23a53f5adbac4c6cd3e4b2ef24140f0a`; this identifies the tested local binary, not a reproducible-build guarantee.

Raw transcripts, account data, runtime configurations and generated patches stay in private local storage and are excluded from Git. Only this sanitized aggregate and credential-free fixture are published. Before expanding the experiment, add harder independent repository tasks, randomize run order, verify model/effort at the serving boundary where possible, and retain failures. No Opus or alternative-provider result should be invented to fill an unavailable comparison arm.
