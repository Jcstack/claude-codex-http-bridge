# Claude Code + Codex compatibility record

Checked 2026-10-30. Claude Code is the harness. The local proxy supplies model inference through the Anthropic Messages interface. A capability is only marked verified when it was exercised; support in one release is not a promise about all future releases or third-party plugins.

| Area | Current result | Evidence or boundary |
| --- | --- | --- |
| Model inference and streaming | Verified on macOS with GPT-6.1 Sol, GPT-6 Sol, and GPT-5.6 Sol. | Real completion requests in the bridge evaluation. The local catalog is not an account entitlement test. |
| `/model` | Verified interactive switching among Sol and Terra routes. | The picker is rebuilt from the authenticated local `/v1/models` catalog at session start. Restart the session for new catalog entries. |
| `/effort` | Low through max are offered only for catalog rows advertising all five levels. | A credential-free [round-trip fixture](tests/probe_effort_roundtrip.py) checks five CLI values through the real client, candidate translator and fake Responses origin; the macOS interactive `/effort` path was checked for all five with Sol, Astra and Luna mappings. Windows interactive behavior and each real model's entitlement remain separate checks. Claude Code's `ultracode` is its own workflow setting, not a Codex reasoning level. |
| `/autocompact` | Reading the configured window works. New profiles derive it from maximum-context metadata. | Earlier profiles used 400k/200k from default windows. A command cannot override their higher-priority startup setting, and editing the file does not hot-update it. Live editing remains a gap; see [OPERATIONS.md](OPERATIONS.md). |
| Claude HUD and other status-line commands | The temporary adapter corrects the selected model's context size and percentage after `/model` switches. A run against the user's installed HUD showed `500k` and the actual Codex weekly quota while suppressing a false 5-hour value and Claude cost. | The adapter withholds Claude-list-price cost and Anthropic prompt-cache timer. Quota follows one enabled bridge account, using matching native Codex RPC or its fixed ChatGPT usage endpoint; disabled backups and stale other-account caches are excluded. It leaves the user's plugin command and settings file intact. |
| Read, Edit, Bash, Agent, skills, hooks, local plugins, MCP | These are Claude Code harness features; a sample of tools, ToolSearch, and MCP was exercised on macOS. | The original pin drops mid-conversation `tool_addition` and loads deferred schemas eagerly. The reviewed managed runtime fixes both and is activated for new launches on this installation. [Runtime rollout](RUNTIME.md), [community review](COMMUNITY_REVIEW.md). [Claude Code's provider matrix](https://code.claude.com/docs/en/feature-availability) lists local subagents, hooks, commands, skills, plugins, and MCP across providers. Individual plugins may still assume a Claude account or specific telemetry. |
| WebSearch and WebFetch | Exercised in the bridge evaluation. | Web search depends on the route and account policy. Claude Code's tool is distinct from every OpenAI-hosted Responses tool. The bridge's per-launch hook replaces WebFetch's known binary-PDF summary with a warning and saved-file path; it does not extract or delete the original PDF. [Claude Code issue](https://github.com/anthropics/claude-code/issues/90416). |
| Native search state and URL citations | Activated managed runtime preserves search items and native URL annotations, including streaming, non-streaming and real Claude resume. | A live installed-TLS search retained its citation across a backend reload. URL normalization matched its source receipt, but does not prove quotation accuracy. Source excerpts, file/container citations and complete citation UI equivalence remain open. [Evidence](OPERATIONS.md). |
| Images and structured output | Exercised in the bridge evaluation. | Audio/video input has not been integrated or verified in this Claude Code path. |
| Native Windows | Installer, archive verification, ACLs, dynamic model logic, and command startup are checked in Windows CI. | Live Windows OAuth/model inference still needs an authenticated Windows run. WSL follows the Linux path. |
| Direct OpenAI API profile | Credential-free mock of generated `api-keys.codex` config, native Responses, model context override, tools, WebSearch, and progressive SSE. | Real project-key inference and billing remain unverified; the API profile does not use the subscription quota HUD. See [API_ROUTE_RESEARCH.md](API_ROUTE_RESEARCH.md). |
| Claude Code `/context` and native cost figures | Claude Code can still report the mapped Claude model's 1M window and list-price dollars internally. | The per-model compact window protects the conversation, and the status-line adapter corrects HUD. The CLI's internal `/context`, `/usage`, and budget calculations are not rewritten by this project. `claude-codex usage` reads actual quota for the selected bridge account. |
| Live Codex account selection | `account login/check/use/list/recover` stages independent authorization and changes subsequent requests without replacing the origin, ingress or Claude processes. | A live old-account generation crossed the switch and completed; new-account tool/search continuation passed. Native Codex auth was unchanged. Prompt cache reuse and already-open native WS connections do not migrate automatically. [Account guide](ACCOUNTS.md). |
| Resume/continue model selection | The launcher restores the last observed parent GPT model for an exact local session ID, a unique observed title, or the latest session in the current directory. Explicit `--model` wins. | Private HUD metadata captures later choices; bounded transcript metadata is a fallback. No prompt text is stored. The native picker is left without a forced model when its selection is not known. A real Claude client fixture verifies Terra survives compact/resume and continue. Old/ambiguous titles outside available metadata are left to the native client. |
| Claude-account cloud features | Not supplied by the localhost model endpoint. | Claude Code disables features such as Remote Control and server-managed settings when `ANTHROPIC_BASE_URL` is not Anthropic's API; other cloud features require a claude.ai sign-in. See [feature availability](https://code.claude.com/docs/en/feature-availability). |
| Optional native compaction | `--native-compaction` uses Codex V2 state through Claude's compact boundary on the reviewed subscription runtime. | A real 64k-input-token fixture compacted twice and retained 12/12 fields after switching to Terra. Normal max effort, tools and fresh images survive replay; native failures retain the original summary path. Opaque summaries and local token estimates have boundaries. [Guide and evidence](NATIVE_COMPACTION.md). |
| Background `/background` and `--bg` | The private localhost service stays running when the foreground launcher exits, so detached work retains its model endpoint. | Start/reuse/stop is exercised in cross-platform smoke tests. An authenticated macOS `--bg` task completed and logged an isolated `PONG` response after the launcher exited. Run `claude-codex bridge stop` only after background agents finish. |
| OpenAI-hosted Responses tools | Not automatically exposed as Claude tools; complete tool loops remain unverified. | The [SIWC limits](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) apply specifically to its public API flow. This installation uses the Codex backend instead; do not infer its full entitlement from SIWC. [Route correction](OPERATIONS.md). |
| Browser MCP | Verified with a pinned, isolated Playwright MCP browser session against `example.com`; the Codex-backed Claude Code session called `ToolSearch` then `mcp__playwright__browser_navigate`. | [Claude in Chrome](https://code.claude.com/docs/en/chrome#prerequisites) and Claude's own [computer use](https://code.claude.com/docs/en/computer-use) require a direct Anthropic subscription. See [BROWSER.md](BROWSER.md). |

## Model comparison relevant to this bridge

| Aspect | Codex route | Claude models in Claude Code |
| --- | --- | --- |
| Context visible to this installation | Corrected: `context_window` is a default and `max_context_window` is the supported override ceiling. Current Astra metadata is 272k / 872k; Sol/Terra 6.1 is 500k / 872k. New profiles use the maximum dynamically. A live Astra request accepted 280,346 input tokens. [Evidence and correction](OPERATIONS.md). | Claude Code's native alias UI can still display 1M; HUD and generated compaction settings use the actual selected Codex maximum. |
| Reasoning controls | GPT-6.1 Sol supports `low`, `medium`, `high`, `xhigh`, and `max`. | Claude models use adaptive thinking and model-dependent default effort. Similar effort names do not imply equal reasoning work. |
| Documented positioning | OpenAI positions Astra for its most demanding tasks, GPT-6.1 Sol for complex work balanced against time and cost, Terra for visual/computer-use work, and Luna for efficient scoped work. | Anthropic positions Fable for demanding long-horizon reasoning, Opus for long-running coding/knowledge work, Sonnet for speed and intelligence, and Haiku for high-throughput tasks. |
| Billing display | A ChatGPT-plan route's quota and bill are not Claude list-price dollars. This bridge hides those dollars from the external status line. | Claude Code can calculate list-price or managed contract-rate figures for supported Claude providers. |

Sources: [GPT-6.1 Sol model page](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [OpenAI model selection](https://developers.openai.com/api/docs/guides/model-selection), [Claude model overview](https://platform.claude.com/docs/en/models/overview), [Claude Code model configuration](https://code.claude.com/docs/en/model-config), and [status-line input schema](https://code.claude.com/docs/en/statusline).

Claude Code documents that `current_usage` may be null before the first API
response and immediately after `/compact`. The existing adapter can turn that
unknown state into `0%`. The adapter now preserves null percentages until all
three input counters are available, while retaining a discovered context size.
The [three-platform source gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41446391248)
passed, and this adapter was installed on the local status-line path without
restarting the origin, ingress, or Claude clients.

An isolated [HUD observation](tests/probe_hud_compaction.py) with the locally
installed Claude HUD 0.8.0 used only synthetic frames. Its percentage helper
returned `0%` when invoked without a transcript or compact hint, even when the
adapter supplied null. With a synthetic recent compact boundary and post-token
hint, HUD returned `6%` for 51,271 out of 872,000 tokens and kept
`current_usage` null. In the same synthetic observation its `both` formatter
displayed zero tokens beside that nonzero percentage. The plugin's actual
renderer produced the same combination in the synthetic probe. Without a
compact hint, its fallback also restored the prior synthetic usage snapshot;
that can be stale. The adapter must not invent fresh/cache token breakdowns to
hide these plugin limitations. The test uses selected overrides and does not
cover every UI layout or other plugin versions. The post-token hint
is not a provider usage report. The probe executes the explicitly supplied HUD
JavaScript with a temporary home and configuration; it is not an OS sandbox.

An isolated [0.15.0 render fixture](tests/probe_hud_render.py) also exercises
the bridge status adapter and a synthetic transcript. On this Mac, the selected
candidate displayed the model, discovered context, weekly usage, Git branch,
tool, MCP, skill, agent and todo; after a synthetic compact boundary with
51,271 post-tokens, it displayed `6% (51k/872k)` rather than `6% (0/872k)`.
The hint is not a provider report. This does not establish every plugin or
session behavior, and the candidate's security and rollout are separate gates.
Those render checks also passed on three platform runners in the [candidate run](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41451870244),
whose Windows security fixture failed separately. The local plugin now uses
0.15.0 with Git display temporarily disabled and a separately documented Bun
launcher patch; see [SECURITY.md](SECURITY.md).

A community [mixed-model subagent report](https://github.com/anthropics/claude-code/issues/83355)
describes an older client using the parent's compact window for a smaller
subagent. In a separate local [synthetic probe](tests/probe_subagent_compaction.py)
with the locked Claude Code 2.1.316 client, a child set to 100k compacted after
a fake 160k usage report whether its parent used 100k or 800k; an 800k child
did not compact when its parent used 100k. The child also completed two
synthetic Read calls. The sample exercises explicit per-model settings and
fake token counts, not an actual long conversation, all agent types or a
provider-enforced context limit. The [source gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41449104553)
passed these synthetic cases on macOS, Linux and Windows.

For future models with fewer reasoning levels, Claude Code documents a
per-model [`maxEffortLevel`](https://code.claude.com/docs/en/settings-reference#maxeffortlevel).
In a credential-free Claude Code 2.1.316 fixture, a `high` cap changed an
explicit `max` request to `high`; a separate uncapped model still sent `max`.
The cap also allowed `medium`, so it cannot by itself represent a hypothetical
non-contiguous set such as `low` and `high`. This bridge has not relaxed its
current full-effort eligibility requirement or silently capped user requests.
Any future partial-capability support needs clear selection behavior and
end-to-end tests across UI, subagents, switching, and upstream metadata.

Claude documents alias-level [`_SUPPORTED_CAPABILITIES`](https://code.claude.com/docs/en/model-config#customize-pinned-model-display-and-capabilities)
flags for pinned third-party models, including `xhigh_effort` and `max_effort`.
Its [gateway protocol](https://code.claude.com/docs/en/llm-gateway-protocol#feature-pass-through)
states that these flags do not apply to an `ANTHROPIC_BASE_URL` gateway. In a credential-free Claude
Code 2.1.316 fixture using this project's custom model picker, setting the
Sonnet alias capabilities to `effort,thinking` did **not** suppress `xhigh` or
`max` requests: both explicit CLI choices and interactive `/effort` reached
the fake upstream unchanged. In the CLI fixture, omitting `behavesAs` or using
the Sonnet alias did not change the observed `max` request. These are specific
synthetic configurations, not a claim about every provider or client version;
they show that those flags alone do not safely enforce this bridge's dynamic
per-model effort set. The existing eligibility gate remains unchanged.

A separate synthetic rejection test exposed a distinct risk even when the
catalog advertises every level. Claude Code 2.1.316 retried a recognizable
HTTP 400 for `output_config.effort` without any effort value, then continued
omitting it on the next turn in the same process. Through the previous
reviewed translator, a fake upstream saw `max` on the rejected request and
`xhigh` on the subsequent request. These were fake errors and do not establish
that the real Codex backend has rejected the user's effort. A narrow guard now
surfaces selected recognizable pre-header rejections on the managed runtime.
It passed [credential-free three-platform tests](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41425623062)
and was activated by rolling backend handoff on 2026-11-02. Legacy sessions
connected directly to the original endpoint still use the older translator.
In a separate direct-client control, a differently worded 400 was visible as
an error and retained `max` on all three synthetic attempts, even with
`x-should-retry: false`. That header alone did not eliminate retries in this
fixture, so any guard must check actual behavior rather than promise one try.

The [Claude effort documentation](https://platform.claude.com/docs/en/build-with-claude/effort#change-effort-mid-conversation)
also describes a beta system-message directive. It applies immediately after
new user input or otherwise waits for the next new user input; a user message
containing only tool results does not activate a pending change. A synthetic
request to the previous translator kept top-level `high` when a
later directive selected `low`. There is no evidence that the user's actual
session sent this directive. The managed runtime now applies the effective level
without editing message history, preserves a pending directive across
tool-result-only turns, and returns an explicit error for malformed directives.
The same runtime intercepts selected pre-header effort rejections so the
tested client surfaces an error while retaining `max` on its three attempts.
Synthetic tests check the cache key, translated tool definitions and input
prefix, but do not prove a real provider cache hit, model-level acceptance,
or identical behavior in later Claude Code versions.

The official [Anthropic system-message specification](https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages)
states that a turn-scoped instruction stops rendering after the next user
message, including a tool-result-only message. The [OpenAI Responses reference](https://developers.openai.com/api/reference/go/resources/responses)
assigns `developer` and `system` messages higher priority than `user`
messages. In an isolated local fixture, the installed translator rendered
an expired synthetic instruction and emitted active system text as a `user`
message. This establishes a wire-level mismatch, not that a real session was
affected or that model behavior changed in a particular way. A separate
[experimental patch](upstream/patches/midconversation-system-fidelity.patch)
emits active synthetic instructions as `developer`, suppresses an expired
instruction, and rejects invalid scoped combinations and placement. Existing
translator tests and synthetic tool and cache-prefix fixtures passed locally.
The candidate is deliberately excluded from the active runtime bundle until
its private Codex backend behavior, client/plugin interaction, cache effects,
and cross-platform tests are independently validated.

Claude Code's [native gateway model discovery](https://code.claude.com/docs/en/llm-gateway-protocol#model-discovery)
is an additional constraint on dynamic GPT discovery: the documented picker
accepts discovered IDs containing `claude` or `anthropic`, caches results,
and refreshes them at startup. A `modelPicker` using `replaceBuiltInOptions`
hides discovered rows. The bridge currently builds its picker from the real
account catalog instead of inventing Claude-shaped IDs or writing an internal
Claude cache. It does not yet update the startup picker or context settings of
an already-running Claude process when the account catalog changes.

There is no controlled head-to-head **of this bridge**. Independent same-harness model results and paired harness evidence are assessed in [MODEL_HARNESS_EVIDENCE.md](MODEL_HARNESS_EVIDENCE.md); the proposed direct API-route acceptance tests are in [API_ROUTE_RESEARCH.md](API_ROUTE_RESEARCH.md). To compare on a specific repository, use the same task, effort budget, tools, and acceptance tests, then inspect the resulting code and time to completion. The bridge's compatibility alias is a Claude Code UI/capability mapping; it does not change the underlying Codex model.
