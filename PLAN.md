# Claude Code × Codex capability and reliability plan

Updated 2026-11-02. Work is guided by the long-running session and its child-session records. Update the evidence, open problems, and next steps after each phase.

## Goals and boundaries

- Claude Code remains the harness and retains local commands, permissions, hooks, skills, MCP, plugins, plan mode, subagents, and background work.
- Use validated protocol adaptation and local tools to expose Codex/OpenAI reasoning, context, caching, search, vision, computer interaction, and streaming capabilities.
- Discover model lists, context limits, effort levels, and capabilities from the current account and upstream metadata; do not make historical model parameters permanent rules.
- Do not add useless content, weaken task quality, lower user-selected effort, or relax permissions to improve cache reuse.
- Keep raw logs, prompts, project paths, credentials, native compaction state, and runtime configuration on the local machine. The repository contains implementation, synthetic tests, and necessary findings only.
- Keep existing sessions working. Prepare and validate replacement translators before switching traffic, and allow old requests to drain. Killing the original listener is not a disruption-free restart.
- Leave the Project API route disabled. Do not invent Opus/Fable comparisons without a Claude account or equate credential-free CI with a real Windows account test.

## Method

Record the same evidence for each capability: user scenario, harness behavior, model/account/route, official references, community maintenance and issues, protocol tests, actual execution, privacy boundary, and known limitations.

Classify results as verified, adaptation needed, unverified, or blocked by an upstream or environmental prerequisite. HTTP 200, a similar model name, a listed tool, or one successful demonstration does not establish complete acceptance.

Start each iteration with read-only diagnostics of the tracked session and its children, then prioritize improvements relevant to real work. Distinguish request failures, interrupted SSE streams, expected test failures, application errors, and tool adaptation problems. Do not add records of the same request from two proxy layers together.

The tracked session's anonymous marker is stored locally in the private installation's `runtime-audit/target.json`, preventing temporary test sessions from being mistaken for it. That file is not committed.

## P0: Availability and investigation

- [x] Identify the long-running main session matching the active process and inspect its main and child logs from startup, extracting only necessary structure and statistics.
- [x] Fix stale 429 responses after usage resets: check the selected account's actual availability, release its expired account-wide cooldown, and stop duplicating the origin's rate-limit state in the translation layer.
- [x] Add a single-instance background check and `claude-codex quota recover` without switching accounts, renewing tokens, running inference, or restarting inference services; retain genuine rate limits and authentication failures.
- [x] Complete the three-platform end-to-end gate for the 429 fix; subsequent requests in the real session continued and background checks worked.
- [x] Create a repeatable local audit for the full lifecycle, child sessions, tool calls, requested model/effort, compaction, and error categories. Report parser limits, skipped records, and missing data; see [SESSION_AUDIT.md](SESSION_AUDIT.md). Categories do not prove root cause, and command records do not prove complete support.
- [x] Identify interrupted streams after HTTP 200 from real session and two-layer access records; diagnostics separately retain handshake and incomplete-stream failures rather than treating 200 as completion.
- [x] Use an isolated real Claude client and the current translator to exercise synthetic failures during partial arguments, completed arguments, completed tool items, and termination. Independent hook counts establish exactly one write in successful cases and preservation of results on continuation; a healthy control must succeed. The three-platform gate does not cover all socket or parallel-tool scenarios.
- [x] Add EOF, `response.failed`, interleaved calls to two tools, and client termination before and after tool completion. Count each tool's effect independently and check subsequent requests for corresponding results; process termination does not represent every Ctrl-C or plugin scenario.
- [x] Send Ctrl-C to a real Claude `-p` client in a private POSIX pseudoterminal before tool completion, after the first upstream item, after one effect while a separate isolated hook blocks the other, and after both effects. The existing local HOME plus installed-path combination timed out; interactive TUI and Windows console behavior remain unverified.
- [x] Resume the same isolated interrupted session with a synthetic model that distinguishes successful and denied outcomes, complete unfinished actions, and verify completed actions are not repeated. This does not cover arbitrary real models or tools.
- [x] Classify recorded incomplete-stream errors using a fixed allowlist. A structured `request_timeout` was observed; its exact source within the origin or farther upstream remains unresolved.
- [x] Compare the running original binary and pinned source to identify the translator's matching incomplete-stream error marker. This strongly associates the wrapper with the message but does not establish why the terminal event went missing.
- [x] Test SSE keepalive, cancellation, truncation, and errors before the first byte with a fake upstream, isolated translator, TLS ingress, and real client. Keepalive before the first byte commits HTTP 200 and moves subsequent errors into the stream; the running configuration was not changed.
- [x] Use a temporary TLS ingress and one short synthetic subscription request to verify keepalive comments and a normal terminal event can reach the client from the current upstream. This does not establish long-stream reliability.
- [x] Reproduce execution of an unconfirmed tool with synthetic `response.incomplete`. The isolated candidate blocks unfinished calls, preserves confirmed calls, and retains request-level errors for HTTP/WebSocket nonstreaming responses. Real-Claude single-tool and parallel-tool negative cases are gated.
- [x] Pass the three-platform gate for that safety patch and roll out the local managed translator. The account, original endpoint, and TLS ingress stayed unchanged, and the old stream drained naturally during handoff.
- [x] Test live settings changes using a real client and two fake gateways. A normal client finishes its old stream and uses a CA-verified new ingress next turn; launch-setting precedence, `--restricted`, and missing-CA negative cases are covered. This does not prove risk-free migration of real sessions.
- [ ] Design safe migration for the original endpoint and legacy sessions connected directly to it; they still use the old translator, and protocols outside the patch cannot be considered fixed.
- [ ] Safely investigate why the local noninteractive Ctrl-C fixture times out with the existing HOME and installed-path combination. The same client passed with a temporary HOME; that does not establish that interactive sessions work or that real user state caused the timeout. See [STREAMING.md](STREAMING.md).
- [ ] Continue testing keepalive, timeout, and safe recovery on the real model path. Distinguish client idle timeout from a missing upstream terminal event; keepalive does not fix the latter.
- [ ] Explain real incomplete streams and verify safe recovery. Distinguish native errors, missing terminal events, disconnects, and cancellation, and check that partial tool calls are neither replayed nor executed twice. Historical causes unsupported by the logs remain unknown.
- [ ] Continue investigating TLS/EOF and 500/503 errors, separating preconnection failures, interrupted streams, client cancellation, and application errors. Do not mask the cause with fixed retries or restarts.
- [x] Add allowlisted URL-host categories for recent handshake EOF mentions, preserving the possibility that errors were forwarded. Real logs mention the official ChatGPT host; later credential-free TLS checks passed, but the root cause remains unknown.
- [x] Add bounded same-log completion correlations for handshake failures, preserving ambiguous and unmatched cases without exposing local tags. One later EOF had a unique HTTP 200 match 12 seconds afterward; neither this correlation nor HTTP 200 proves retry behavior or a complete SSE stream.
- [x] Restore a dead managed endpoint using its existing loopback port, CA, and client key, while refusing occupied ports and avoiding signals to stale PIDs. A disposable synthetic-origin fixture covers a changed origin, recovery failure rollback, and requests after recovery.
- [x] Add opt-in current-user login recovery on macOS and Windows, with local-only healthy checks, serialized manual stop, and retry after transient startup failure. A macOS launch agent was loaded and observed a healthy existing service; no actual computer reboot was performed.
- [ ] Exercise an actual macOS reboot and an authenticated Windows login on their respective hosts, and investigate Linux user-service support. Neither synthetic recovery nor a loaded login entry proves post-reboot behavior on every host.
- [x] Implement controller-side process creation identity for new origin state and verify the executable before adopting legacy state. Local synthetic tests cover a reused PID/port, a changed creation identity, and an unrelated listener without exposing local keys. Windows and Linux source/install jobs passed; the existing macOS origin was enrolled without replacing its listener or process.
- [ ] Extend process birth verification to each Go ingress-to-origin request without disrupting existing streams. The current ingress checks only PID and port at that boundary.
- [x] Restrict new origin and isolated-account-validator environments so unrelated shell credentials and upstream remote-store selectors are not inherited. Preserve explicitly configured network proxies for the normal origin; the validator retains its direct-network policy. The existing origin continues with its original environment until its next natural restart.
- [x] Account for the macOS login environment losing shell proxy variables. Capture only matching system-proxy schemes and the shell's bypass rules in a private local policy; retrieve proxy addresses in memory at startup and refuse a missing expected proxy. A launch-like isolated subprocess verified the current machine's system proxy values reach the origin environment, and a local Go proxy-selection check used that environment without contacting either destination. This does not establish connectivity after an actual reboot.
- [ ] Validate Windows login proxy configuration with a real user session, and support or clearly diagnose PAC, SOCKS-only, and bypass configurations that cannot be represented safely. Local metadata health is not an Internet connectivity test.

Acceptance: reproduce successful requests and genuine quota-exhaustion negative cases; automatically recover after a reset; avoid duplicate tool effects; preserve the original account, listener, keys, sessions, and already-started streams.

## P1: Prompt caching, long context, and compaction

This remains a priority. The real session provides evidence of cache reads and also shows a long compaction; evaluate those separately.

- [x] Establish a deduplicated cache baseline for the long-running session. Cached-token share is not request hit rate, cumulative input is not one context window, and missing counters do not prove free usage.
- [x] Check current official caching documentation and the installed translator. Cache read/write counters are translated, but routes handle cache-control fields differently; field names alone do not establish availability.
- [x] Test cache reads on the current subscription route with synthetic fixed prefixes and record first-text and total latency, including hits and misses. See [CACHE_AUDIT.md](CACHE_AUDIT.md). Quota impact and a full performance comparison remain unverified.
- [x] Bypass translator filtering and test the current Sol subscription backend directly through the original credential process. Ordinary requests succeeded; explicit and implicit `prompt_cache_options` and standalone breakpoints were rejected with HTTP 400. Retain compatibility filtering and do not portray automatic caching as working manual control.
- [x] Test ToolSearch loading/removal against a synthetic API-profile upstream for tool-definition ordering and bytes, instructions, and the historical input prefix. Cache keys stayed stable within one synthetic session and differed across synthetic sessions; this does not prove subscription-backend hits or account isolation.
- [x] Use synthetic credential metadata and a local upstream to test the subscription executor's tool loading/removal, stable prefixes, session/subagent/model cache-key scope, and effort preservation. This does not verify real account isolation or provider cache hits.
- [x] Observe cache counters around tool discovery and removal with two short synthetic histories through the real subscription gateway. Retain samples without reported reads; do not infer hit rate or behavior of other accounts.
- [x] Repeat the same prefix across two synthetic sessions on one account and observe differences in cache counters. The result does not establish provider isolation across sessions or accounts.
- [x] Load one and nine deferred tools with a real Claude client and synthetic MCP/Responses upstream, then inspect translated cache prefixes. Multi-tool cache reads at the real provider remain unverified.
- [x] Switch five effort levels in macOS interactive synthetic sessions using Sol, Astra, and Luna compatibility mappings, checking structural preservation of the cache key, nonempty tool definitions, and historical input prefix. This does not prove provider hits or cross-account isolation.
- [x] Align a recent long compaction with shared original-endpoint access records and identify a request lasting nearly the entire compaction. Without cross-layer request identity, this is correlation, not attribution to model computation or a network component.
- [x] Compare processing latency and integrity for synthetic 100 KB, 1 MB, and 4 MB messages using an independent local TLS ingress and fake upstream. Byte size and repeated text are not equivalent to real context tokens or model computation.
- [x] Send native-compaction progress events from a fake upstream and delay termination; the current optional native-compaction path sends no body to Claude before termination. Do not generalize to default summary compaction or attribute the real slow request to this observation.
- [ ] Check the expressible range of newer Responses cache options, breakpoints, TTL, cache keys, and Claude `cache_control`. Enable them only when the selected route supports them; do not assume public API behavior applies to the Codex subscription backend.
- [ ] Cover stable system prefixes, tool ordering, ToolSearch dynamic loading/removal, tool results, model/effort changes, account isolation, resume/fork, compaction, and invalidation boundaries.
- [ ] Check preservation of reasoning and native state, cache-write double counting, and whether the UI incorrectly applies Anthropic cache durations or prices to this route.
- [ ] Compare ordinary summaries and native compaction on larger synthetic tasks with more tools and more complex constraints; assess later code acceptance, memory, plugins, images, and tool results. Retain failures and slow samples.
- [ ] Reconcile discovered default/maximum context, generated compaction windows, Claude's internal reserve, native token estimation, and HUD. Do not disable compaction to improve the display.
- [x] Use one account lock for account switching and new-session preparation. Validate the target account's model, context, and effort catalog snapshot; prepare and recheck a new translator when it changes. Isolated tests cover unchanged and changed catalogs, rollback after a prepublication change, lock contention, and completion of an old stream. The current real account catalog matched in a read-only check.
- [x] Review the pinned upstream's internal per-credential dispatch. Credential-free source tests cover streaming/nonstreaming dispatch and absence of fallback for a missing or disabled nonstreaming credential; synthetic streaming tests also show explicit selection takes precedence over session affinity and does not fall back for missing, disabled, or simulated rate-limited credentials. This is not an exposed HTTP account-selection capability.
- [x] Observe `/model` in a temporary POSIX interactive terminal: a synthetic new model in user settings appeared live, but changes to a startup `--settings` file did not refresh. This client sample does not establish that shared user settings are safe for real sessions.
- [ ] Eliminate the inconsistency window that new requests can encounter between account switching and catalog publication. Investigate safe updates to an existing Claude process's startup model picker and context settings without restarting it; neither is established yet.
- [ ] Separately handle account differences in input modalities and other model capabilities. The running translator's comparison covers model IDs, context, and effort; it does not prove that image or other capability displays in existing clients follow an account switch.
- [x] Check five effort levels for Sol, Astra, and Luna compatibility mappings using an isolated real Claude client, candidate translator, and fake Responses upstream. CLI settings and macOS interactive `/effort` arrived unchanged at the simulated upstream. This does not prove that every real model/account supports every level; Windows interactive terminals remain unverified.
- [x] Test Claude's documented per-alias supported-capabilities flags with the custom picker and synthetic partial effort sets. In the tested client, restricting the flags to `effort,thinking` did not stop CLI or macOS `/effort` from sending `xhigh` and `max` to the fake upstream. This does not prove behavior for other clients or providers; the production gate remains unchanged. See [COMPATIBILITY.md](COMPATIBILITY.md).
- [ ] Design visible, verifiable choices for future models supporting only some effort levels. Official per-model limits work in an isolated client but clamp excessive requests and cannot alone represent discontinuous supported sets; do not relax the gate on that basis.
- [x] Prevent selected recognizable pre-header effort rejections from silently dropping an explicit reasoning choice in the managed runtime. Synthetic direct-client and translator tests reproduced `max` changing to an omitted client field and then `xhigh` at a fake upstream; the guarded client retained `max` across three failed attempts. The [three-platform candidate gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41425623062) passed and the backend was rolled without replacing the origin or ingress. This does not establish that the real backend had rejected the user's effort; direct legacy sessions remain on the previous translator.
- [x] Reproduce a separate mid-conversation effort gap with the installed translator and a synthetic Responses origin: a `low` directive after top-level `high` arrived as `high`. This is not evidence that the tracked session used such a directive.
- [x] Validate and roll out the managed mid-conversation effort mapping. Synthetic three-platform tests cover immediate and pending changes, consecutive updates, tool-result-only turns, malformed input, structural cache prefixes, and unchanged normal `/effort`; actual provider cache behavior, model acceptance and new Claude client releases remain separate checks.

Acceptance: repeatable positive and negative cache cases, correct token accounting, protected account boundaries, and passing task acceptance after compaction. Compare performance on the same tasks and comparable budgets; do not sacrifice correctness for cache gains.

References: [OpenAI Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching) and [Claude tools and prompt caching](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching). Public API controls, durations, and pricing need separate validation; the Project API is currently disabled.

## P2: Claude Code local capabilities and plugin matrix

- [ ] Cover `/model`, `/effort`, `/compact`, `/context`, `/usage`, plan mode, resume, fork, rewind, context editing, and background work individually.
- [ ] Verify permission denial, hook blocking, skills, coexisting MCP servers, dynamic tools, partial tool-argument streams, structured output, cancellation, and retries.
- [x] Reproduce MCP original-name normalization collisions and normalized-alias permission controls with an isolated Claude client and fake Messages service, without connecting real plugins. See [MCP_NAMING.md](MCP_NAMING.md).
- [x] Provide a read-only offline MCP name audit for potential server-name collisions in local configuration and user-provided tool snapshots. It prints no names by default, connects to no servers, and explicitly reports partial coverage.
- [x] Prototype deterministic pre-client aliases with a test-only transport. An isolated real Claude client separately called two formerly colliding synthetic tools, enforced a per-alias deny, resumed the synthetic session, and fetched and called a changed synthetic tool list. A fake Responses origin and reviewed translator also exercised ToolSearch and structural cache preservation. This is not a production transport or a real-plugin test.
- [x] Inspect maintained community gateways and test the already-locked MCP SDK with synthetic JSON-RPC messages. A top-level extension was dropped and one invalid synthetic input appeared in a captured parser error log; neither observation proves behavior for all messages or SDK configurations. The desktop adapter now filters that specific SDK logger before client startup; child stderr, other loggers, and running processes remain outside the filter. See [MCP_NAMING.md](MCP_NAMING.md).
- [ ] Resolve original-name normalization collisions before tools reach Claude Code, covering dynamic tools, cross-server collisions, permissions, and resume. The HTTP translator alone cannot recover identities already merged by the client.
- [x] Reproduce a mid-conversation system-message mismatch using synthetic inputs: active system text becomes a user message, and a turn-scoped instruction remains present after the next user message.
- [ ] Validate the separate system-message fidelity candidate against real Codex subscription behavior, client/plugin tool loops and provider cache effects before any activation. Synthetic malformed/placement and protocol cases passed the three-platform candidate gate, but OpenAI's public Responses schema alone does not establish behavior of the private subscription endpoint.
- [ ] Independently investigate file-permission differences in the current client across Windows and configuration sources. The stream-failure gate uses a PreToolUse handler allowing only one synthetic target and includes a denied-other-target control; that configuration is not installed in real sessions.
- [ ] Inspect real child-session records for model/effort inheritance, background wakeups, message collaboration, and tool results; main-session success does not establish all child-session behavior.
- [x] Preserve Claude Code's native subagent default, force switch, and compatibility alias overrides, validating against the available catalog first. A credential-free real client verifies precedence, same-family exceptions, complete model IDs in agent definitions, and one independent effort. Real backend inference for every model and all agent scenarios still needs acceptance.
- [ ] Check Claude HUD and other plugins depending on status lines, session records, or readable summaries, especially after native compaction.
- [x] Restore readable operation names and retain fitting instance prefixes for excessively long gateway MCP names in an experimental candidate. Real Claude, synthetic same-name tools, and mid-session handoff tests passed. Special naming, extreme lengths, real plugins, and cross-platform publication require separate validation; see [COMMUNITY_REVIEW.md](COMMUNITY_REVIEW.md).
- [ ] Establish which Claude internal statistics and startup settings can be modified; do not fabricate Anthropic account state or fill gaps with incorrect alias data.
- [ ] Run locked real-client, protocol, and three-platform gates for new Claude Code/Codex versions, evaluating model and client updates separately.

Acceptance: an actual flow plus permission/error negative cases for each capability; report plugin compatibility by version and scenario without unlimited guarantees for future plugins.

## P3: Complementary Codex/OpenAI capabilities

- [ ] Complete end-to-end native search, source-state replay, citations, precise excerpts, and file/container references.
- [ ] Verify actual PDF, image input/generation/editing, dimensions, and transparency contracts, distinguishing provider originals from local transformations.
- [ ] Extend scoped browser and selected-window flows while preserving observation lifetime, coordinates, action limits, and permission boundaries. Do not connect the everyday browser or open the full desktop without authorization.
- [ ] Separately test Realtime, GPT-Live, full duplex, video/screen streams, and mid-turn steering. Retain both the successful Live creation and failed media test; these do not establish availability.
- [ ] Design adaptations around Claude Code tool and interaction boundaries without replacing the harness with an additional Codex coding agent.

Acceptance: the model receives actual input, executes authorized tools, and returns correct results; media contains real data; interrupted, denied, and cancelled streams close correctly. Explicitly retain prerequisites for unknown permissions or unavailable upstream features.

## P4: Coding quality and model/harness differences

- [ ] Expand reproducible offline coding tasks to cover multifile changes, debugging, dependency understanding, test repair, long-task continuation, vision, and tool selection.
- [ ] Measure model, harness, adaptation, and caching effects separately using independent acceptance, the same starting point, budgets and tools, and repeated samples. Record the difference between requested model and verifiable actual identity.
- [ ] Retain the current experiments: small samples passed with both native Codex and Claude Code but do not prove Claude Code is generally superior; code repair after native compaction is also only one passing case.
- [ ] Add comparisons of GPT, Claude, and other candidates using official and reproducible community results, examining methods, versions, maintenance, and issues. With no authorized access, assess documentation only.

Acceptance: independent tests can reproduce conclusions; one success, community popularity, or scores under different budgets do not establish Opus/Fable parity or a “150%” improvement.

## P5: Security, Windows, and ongoing maintenance

- [ ] Investigate one 502 after a generation handoff in the credential-free Windows candidate gate. A later rerun passed and the cause is unknown; the test now records sanitized process-liveness, elapsed-time, and error-category diagnostics only.
- [ ] Test real accounts, models, HUD, browsers, resume, and long-running behavior in a private Windows environment; credentials must not enter public CI.
- [x] Give the credential-free real Claude fixture a temporary HOME, including temporary user configuration and application-data paths on Windows. An isolated environment is not an OS sandbox.
- [x] Restrict subprocess environment and HOME for temporary API protocol, dynamic-tool, and installation-smoke proxies, and for clients, proxies, and temporary ingress in subscription-candidate tests. Fake-upstream flows passed; subscription-dependent tests were not rerun in that iteration.
- [x] Restrict environment variables for new managed ingress, translators, and TLS initialization to system necessities and private local TLS configuration, avoiding unrelated inherited keys. Roll out the local managed translator after the three-platform gate. The existing TLS ingress, Claude, original authentication service, and any streams still draining were not restarted and retain their previous environments.
- [ ] Audit HOME, inherited environment, system credentials, and external-access boundaries for remaining real subscription probes and other proxy launch paths. Do not generalize isolation findings from updated fixtures to unverified paths.
- [ ] Finish three-platform and real-plugin validation for the gateway MCP name candidate before assessing a safe release. Rewriting historical names in an existing session may change cache prefixes.
- [ ] Prepare a reviewable migration plan for the original listener and ingress themselves. Only the validated translator handoff is currently considered non-disruptive.
- [ ] Add a validated operator-facing rollback for an earlier selected bundle when the current launcher source lock has changed. Earlier immutable bundles remain private on disk, but a normal `bridge upgrade` verifies the current source lock and cannot simply select an older lock.
- [ ] Inspect complete history, runtime-data exclusions, and dependency provenance for each commit. Explain the benefit, maintenance, issues, and security of new dependencies.
- [ ] Record version, tests, actual limitations, rollback, and effects on existing sessions for each release; continue using main without a new branch.

## Next steps

The P0 quota gate, long-running main/child session audit, single/parallel tool stream-failure tests, and POSIX noninteractive Ctrl-C samples are in place. P1 has initial cache-read comparisons, real cache samples after dynamic tools, negative tests for manual controls rejected by the current backend, and timing correlation for long compaction. Account-catalog synchronization has isolated coverage, but the new-request handoff window and existing client settings remain open. Fake gateways covered some settings reload and TLS negative cases; independently reversible migration of a real session remains unproven.

Continue investigating native stream errors, interactive terminals, Windows interruptions, and other tools. Compare native compaction and ordinary summaries for quality and latency without affecting active sessions or their quota. Do not enable cache fields rejected by the backend. The MCP alias policy and fixture transport remain experimental: assess production framing and lifecycle, dynamic changes, real plugins, permissions, cross-server names, and Windows before any opt-in deployment. Adjust priorities as real session evidence changes, and validate changes in isolation before applying them to a stable ingress.
