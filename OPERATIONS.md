# Runtime verification and release work

Checked 2026-10-30. Existing user sessions and the authenticated service remained running. Temporary proxies received only a private localhost client key. No OAuth file, OpenAI project key, raw user transcript or production trading file entered these tests or this repository.

## Context correction

The earlier bridge incorrectly treated `context_window` as the maximum. Both the authenticated catalog and native Codex cache distinguish these fields:

| Observed route | Default | Maximum | 90% of maximum |
| --- | ---: | ---: | ---: |
| GPT-6 Astra | 272,000 | 872,000 | 784,800 |
| GPT-6.1 Sol / Terra | 500,000 | 872,000 | 784,800 |

The corrected discovery uses a valid `max_context_window`, preserves the default and source field, and rejects inconsistent metadata. Values remain dynamic. API metadata cannot inherit a subscription-only maximum. [Codex's model definition](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/protocol/src/openai_models.rs#L461) distinguishes default, maximum, usable context and the 90% compaction calculation; [its override code](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/models-manager/src/model_info.rs#L20) clamps a requested context to the maximum.

A live Astra request accepted **280,346 input tokens**, retained both boundary markers and completed in **12.111 seconds**. This disproves a 272k hard limit here; it does not exercise all 872k tokens.

The generated `autoCompactWindow` is 90% of the selected context or a lower explicit native limit. Claude also reserves space internally, so 784,800 is a configured window, not an exact trigger guarantee. Claude Code 2.1.314 caps this setting at 1M; only the setting is capped, not model metadata. Running sessions retain startup settings. Isolated persistent-client tests found that editing `--settings` on disk did not update the running window, and `/autocompact 784800` could not override its higher-priority 400k setting. A later launch/resume with the corrected profile is required. Live editing of generated compaction defaults is a remaining client integration gap.

OpenAI's [Codex pricing exception](https://help.openai.com/en/articles/20001415-chatgpt-rate-card-enterprise-token-based-pricing) confirms that ChatGPT-signed-in Codex has no long-context multiplier, including above 272k, and no cache-write charge. Self-supplied API keys follow API pricing. Long requests still consume usage; API dollars do not measure included subscription allowance.

## Compaction and caching

The reported long-session incident completed without intervention in **589.457 seconds**, reducing **366,956** tokens to **35,307**. Its gateway request lasted about 589 seconds and returned HTTP 200. Earlier automatic compactions in that session took roughly 7–12 minutes. This establishes a slow completed request, not an endless client retry loop. HTTP 200 alone cannot exclude a later SSE error.

The [live compaction fixture](tests/live_compaction.py) preserved an updated synthetic configuration across compaction and resume:

| Effort | Before / after context | Compaction duration | Exact state retention |
| --- | --- | ---: | ---: |
| max | 14,214 / 2,489 | 115.773 s | 12/12 fields |
| high | 14,076 / 2,159 | 56.028 s | 12/12 fields |

One run per effort, max first, is not evidence of equivalent quality on a 367k-token investigation. No live user effort was reduced. The `/compact` command reported zero usage counters; that is not proof of zero quota consumption.

The [cache fixture](tests/live_cache_context.py) used a stable Claude-style session identity. Sol reported 12,346 total input tokens each time: zero cached on the first request, then **12,160 cached + 186 fresh** on both subsequent requests (**98.49%** cached input). A control without session identity read zero cached tokens on all three requests; it was not a faithful Claude session. These are observed hits, not guaranteed lifetime or hit rate. Cached input still occupies context.

New launches default `CLAUDE_CODE_ATTRIBUTION_HEADER=0` and `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`, respecting explicit overrides. The [official gateway guide](https://code.claude.com/docs/en/llm-gateway-protocol#system-prompt-attribution-block) recommends client-side attribution omission for third-party cache compatibility. Its [hint headers](https://code.claude.com/docs/en/llm-gateway-protocol#gateway-hint-headers) identify compaction without guessing from prompts. The client probe verified both flags and unchanged `max` effort. No compaction-effort override is installed.

OpenAI's current [API cache guide](https://developers.openai.com/api/docs/guides/prompt-caching) distinguishes GPT-5.6-and-later `prompt_cache_options.ttl` (`30m`/`12h`) from older `prompt_cache_retention` settings. Its API write prices must not be imported into Codex subscription accounting. The inspected [Codex 0.166 request schema](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/codex-api/src/common.rs#L263) includes a cache key and reasoning context but not those TTL controls. This bridge does not claim API TTL parity for the subscription backend or set an unverified retention policy. Compaction changes the prefix; model switches, expiry and upstream routing can also prevent reuse. Cache-read pricing and actual latency are separate measurements.

An anonymous transport follow-up sent no credentials, prompts or model requests.
Two trials each with standard environment-aware TLS, direct uTLS and uTLS with
an explicitly supplied environment proxy all reached the fixed Codex catalog
endpoint and returned the expected HTTP 401. Durations were 0.221–1.300 seconds.
This small probe did not reproduce the authenticated stream's TLS/EOF failure,
so it does not establish its cause or justify changing the live proxy settings.

On 2026-10-31 a later read-only inspection found another brief cluster of
upstream `utls: TLS handshake: EOF` failures and five completed HTTP 500
requests. Anonymous standard/environment, direct uTLS and explicitly proxied
uTLS catalog checks all reached 401 twice; a subsequent synthetic authenticated
Astra request completed in 2.441 s. This identifies a preconnection failure
phase but does not establish a root cause or a permanent fix. No proxy, TLS or
credential setting was changed on that evidence. Diagnostics now separately
count upstream handshake attempt mentions and distinct tagged requests;
downstream EOF mentions are not classified as upstream handshakes.

After that cluster, a follow-up window contained four completed inference
requests and no further TLS/EOF mentions. A separately cancelled 499 and the
deliberate GPT-Live route probe's 404 remain in the diagnostics; they are not
silently counted as successful model calls. Recovery during this window is
an observation, not evidence of a permanent transport fix.

The installed discovery/launcher/diagnostic helpers were updated without stopping the service. Existing HUD profiles now reflect the discovered maximum. The status-line adapter suppresses Anthropic-specific cache-expiry and price fields; it retains actual cached-token usage. Arbitrary upper limits on future HUD context windows have been removed. Existing client compaction settings still require a later corrected launch/resume.

## Search state and isolated live handoff

A new regression found that v8.0.46 returned native search sources to Claude but dropped the search output item on the next request. The [candidate patch](upstream/patches/web-search-replay.patch) carries completed native `web_search_call` items through a bounded, typed internal transcript envelope, preserving source fields and encrypted search state without an in-memory lookup table. It does not encrypt public sources or create a credential. User-role content cannot inject an assistant replay item. Other native item types and oversized envelopes are rejected. Duplicate search events no longer emit deltas outside an open SSE block.

[Synthetic HTTP and real-Claude resume tests](tests/smoke_web_state.py) verify exact native-item replay after replacing the disposable proxy process. A real subscription search returned two search calls and 16 source entries in 8.683 seconds; the next process accepted the preserved state and repeated a previously returned URL in 4.878 seconds. One earlier live trial failed a stricter exact-source-URL assertion; the later fixture distinguishes URLs returned in text from the native source list. These observations do not certify citation accuracy. At that stage native output-text annotation conversion remained a gap; the later URL annotation results below supersede it. Full Claude citation UI fidelity remains open.

The [live ingress probe](tests/live_gateway_handoff.py) used two disposable translators behind the stable listener, both calling the existing authenticated localhost service:

| Trial | Old admitted request | Cached input during / after switch | Tool/reasoning continuation |
| --- | --- | --- | --- |
| 1 | Completed; old generation drained | 12,160 / 12,160 of 12,335 input tokens (98.58%) | Tool result retained; trivial call returned no reasoning signature |
| 2 | Completed; old generation drained | 12,160 / 0 of 12,335 input tokens | Arithmetic tool fixture returned one reasoning signature; replay and result continuation succeeded on the other backend |

Trial 1 requests took about 3.6–4.9 seconds; trial 2 took about 18–27 seconds. The second trial demonstrates that correct local handoff does not guarantee an upstream cache hit or low latency. It does not identify why that cache miss occurred. Neither trial switched the user's original service. The managed runtime described below now uses a separate stable ingress; it cannot inherit the legacy listener while that process owns its port.

## Regression gates

The URL-citation follow-up adds a source-locked patch and keeps old, previously
selected immutable bundles valid across launcher source updates. New activations
still require the current source lock. Local Go translator and race tests,
28 Python tests, streaming/non-streaming HTTP fixtures and real Claude resume
passed. The complete managed TLS/background fixture also passed locally.

A live subscription search returned two native search calls, 16 source entries
and one URL citation in 13.843 seconds. A separate new translator accepted the
annotation-bearing history and continued in 7.710 seconds, recalling a source
URL. The annotation URL did not match any native source-list URL byte for byte;
the cause is not established. Preserving a provider citation does not verify its
grounding. Claude's source-quotation field remains empty because the URL
annotation provides no exact source excerpt. The [fifth-patch candidate run](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41078035682) passed macOS, Linux and native Windows, including real Claude resume and the complete managed TLS/background fixture. The source/install run at the same commit also passed.

| Probe | Result | Scope |
| --- | --- | --- |
| [Transport](tests/smoke_transport.py) | Auth before upstream; two pre-output 503 retries; truncated output not replayed or declared successful; disconnect cancels upstream; next request works; quota cooldown retained | Fake upstream; pin and candidate passed locally; included in CI |
| [Claude client](tests/probe_claude_client.py) | Compact/resume, changed model/effort on resume, explicit Read hook, denied Write, compaction hints, attribution omission | Real Claude 2.1.314, fake model |
| [Live candidate](tests/live_candidate_subscription.py) | Dynamic reveal, call/result, removal, real Claude ToolSearch then MCP | Isolated macOS subscription flow |
| [Paired translators](tests/live_compare_translators.py) | Two rounds each, all streamed tool calls and continuations passed | Same Sol/high and extra localhost hop; not a coding benchmark |
| [Diagnostics](tests/test_diagnostics.py) | No service startup; bounded parsing; no bodies, paths or credential strings in output | Synthetic privacy regressions |
| [Model state](tests/smoke_model_state.py) | Encrypted reasoning, commentary phase and call/result pairing survive a new proxy process; stable session cache key; no cached-input double counting; max effort, image and JSON-schema translation retained | Fake upstream, candidate passed; this is not a live-stream restart test |

Individual paired requests took 4.258–6.747 seconds for the pin and 3.741–4.970 for the candidate. The small sample found no latency regression; it does not establish a speed advantage.

## Activated managed runtime

The source-locked [runtime controller](RUNTIME.md) is now implemented and activated on the reviewed local installation. Existing direct-listener sessions still use their original service, which was not stopped or replaced. New `claude-codex` launches use the fixed TLS endpoint and patched translator; later `bridge reload` replaces only that translator. The compatibility backend receives a separate loopback guard key, not the legacy key, OAuth material or an OpenAI project key. Native Responses, model discovery and media routes retain guarded passthrough. Proxy processes run in private directories rather than loading a project `.env`.

The [candidate run at `22112c7`](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41070996073) passed all three operating systems. This includes the real pinned Claude client, exact Windows rollback bytes, active-stream stop refusal, readiness rollback, PID/auth boundaries, same-process conversation across reload, and a background reply through a supervisor started with an incorrect gateway. Those model replies were synthetic. The separate live subscription evidence below uses the installed managed endpoint.

| Live check | Observed result |
| --- | --- |
| [Persistent real Claude](tests/live_managed_runtime.py) | Same process executed `Read`, survived a backend reload and recalled the file value. First turn 8.623s; second 3.705s. Listener identity and legacy state stayed unchanged. The second small request reported no cache read. |
| [Managed cache plus reload](tests/live_cache_context.py) | 12,346 total input tokens each request. First cached 0; both requests after reload cached 12,160 and read 186 fresh (98.49%). Boundary markers stayed correct. Times 2.788s / 3.215s / 5.353s. |
| Native subscription image generation | GPT Image 2.5 Flare returned a valid 969,237-byte PNG in 15.518s. Requested 1024x1024; actual PNG 1254x1254. Generation succeeded, but the size contract did not. No API project key was used and the result was not resized to disguise that difference. |

The [official Image API](https://developers.openai.com/api/reference/resources/images/methods/generate) documents the requested standard size, so the native Codex route's observed image dimensions are a real compatibility boundary. The [repeatable image probe](tests/live_native_images.py) checks PNG dimensions and fails on a mismatch. That direct probe alone did not certify Claude-side image tools, editing, streaming images, desktop control or GPT-Live. The later optional-pack evidence below adds explicit tool-level checks. It corrects the earlier blanket assumption that this subscription backend could not generate images.

The URL-citation runtime was then activated on the same installed TLS listener,
without changing the original authenticated service or its state. A live search
through that endpoint returned two searches, 16 source entries and one native
URL citation in **10.712 seconds**. The backend was reloaded, the same listener
accepted the annotation-bearing history, and source recall completed in **3.355
seconds** with 4,224 cached input tokens. The native citation URL matched a source
receipt after normalization that removes UTM parameters and fragments; raw URLs
are preserved by the bridge. This explains the observed normalized match, not
whether the cited page supports every generated claim. No source excerpt was
invented and no OAuth material entered the translator.

Both cache successes and misses are retained here; no guaranteed hit rate or coding-quality advantage is inferred. Exact source quotations and remaining citation types, slow compaction, upstream transport failures, authenticated Windows inference and broader model/harness evaluations remain open. The original direct listener and the TLS ingress itself have not been seamlessly replaced; the verified reload target is the translator behind the fixed ingress.

## Claude Code 2.1.316 client gate, 2026-10-31

The client fixture is now integrity-locked to 2.1.316. Local macOS checks passed
real Claude compact/resume, changed model and max effort on resume, explicit Read
hooks, denied Write, compaction hint headers, and attribution omission. The
separate MCP check passed image/PDF/Realtime results and coexistence with another
MCP server. These checks use synthetic model responses and no user credentials.
The capability-environment unit suite passed 70 tests with one platform skip.

The [official release](https://github.com/anthropics/claude-code/releases/tag/v2.1.316)
includes fixes for corrupted compressed-response hangs, restricted file reads,
PowerShell hook blocking and inherited subagent models. This release note is not
evidence that every reported gateway failure has been fixed. Existing processes
remain on their loaded version; updating the client executable does not restart
them. Both the [three-platform source gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41159711919)
and [patched-translator gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41159754573)
passed. The official pinned installer then selected 2.1.316 locally; both
already-running Claude processes survived, with the origin/ingress metadata,
TLS CA and local keys unchanged. New launches use the updated executable.

The separate [live coding fixture](tests/live_coding_compaction.py) also passed
16 independent acceptance tests after native compaction and a requested model
change. It uses a disposable source tree and verifies real Read/Write/Bash
activity; no production project, transcript or credential is stored in Git.

## Read-only diagnostics

```text
claude-codex diagnostics --hours 2
claude-codex diagnostics --hours 24 --session SESSION_UUID
```

This command makes no network/model call, never reads a key, and never starts/stops a service. It reads local logs and recent main-session metadata, then emits an allowlisted JSON report. No prompts, credentials, project paths or raw session IDs are printed; tags are hashes. Cache counters deduplicate repeated assistant stream blocks by response ID and separate fresh input, reads and writes. The report includes recent failed requests without raw error bodies. Origin and managed backend logs are reported separately; the same inference can appear in both and their counts must not be added. At most the 32 most recent managed backend logs are inspected. Error mentions are not retry counts, in-flight requests are absent, and the last main turn's effort does not establish compaction effort. Commands, subagents and oversized skipped records may not appear in cache totals.

Native media creation, primary/sideband connections and hangup failures now have
separate endpoint labels. Opaque call/session identifiers and query values are
replaced or removed. A failed voice-cleanup request is therefore distinguishable
from `/v1/messages` and `/v1/responses` failures; none is silently discarded.
The follow-up suite passed 71 local tests with one platform skip, and the
[three-platform source/install run](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41161289097)
passed all 11 jobs. The installed read-only helper matches that reviewed source;
no service, endpoint, signing key or active Claude process was replaced.

## Open sessions and service replacement

The legacy process owns its loopback listener directly. Its [shutdown code](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/sdk/cliproxy/service_lifecycle.go#L256) cancels the runtime context. The stop command also removes runtime key/UI files, so it is unsuitable during dependent tasks.

Two isolated persistent-client experiments changed settings while a response remained open. The next request still used the old endpoint, and `/autocompact` retained the old window. Live file edits are not a demonstrated migration method. The host supports plugin reload, but [native translators take precedence](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/sdk/translator/registry.go#L124); a translator plugin cannot simply replace this native route.

A safe upgrade needs a stable authenticated listener, a ready replacement backend, new-request routing and draining of old streams without replay. Local key, session identity, tool history and reasoning state must survive. The [ingress implementation](gateway/README.md) and [runtime controller](RUNTIME.md) now provide this for new sessions; the live results above extend the tests to real model traffic. The direct listener remains for old sessions. Seamless replacement of that listener or of the ingress process itself is still unresolved. No active process was stopped on the assumption that a quick restart is harmless.

The actual installation uses the Codex backend (`use_siwc=false`, transport errors name `chatgpt.com`), not the public SIWC `api.openai.com/v1` flow. The [SIWC limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) explicitly apply to that public flow. Earlier documents overgeneralized them to every subscription backend. Native hosted tool availability on the current backend needs its own execution tests; it must not be inferred solely from that page. Nor does a Codex desktop feature automatically become a Claude Code tool.

Remaining work, updated 2026-10-31:

- Establish the cause of intermittent upstream TLS/EOF failures. The observed
  cluster recovered; a permanent transport fix has not been demonstrated.
- Migrate/replace the original listener and TLS ingress process themselves
  without disturbing dependent sessions. Translator-only replacement is
  verified; it does not provide file-descriptor handoff for those processes.
- Validate native compaction on diverse large real tasks and summary-dependent
  plugins. Small/64k fixtures, repeated boundaries, installed resume/fork and
  normal max effort and the 16-case coding workflow pass; ordinary summaries
  remain the default.
- Resolve Claude's internal `/context`, `/usage` and budget alias figures and
  live startup-setting changes. HUD uses discovered provider context and actual
  quota; file edits do not hot-update existing client startup settings.
- Complete exact source excerpts, file/container citations, full citation UI
  fidelity, broader file/app workflows and native image-size fidelity.
- Integrate primary GPT-Live/full-duplex voice and mid-turn steering into the
  harness. Realtime media and partial text/tool streams are already exercised;
  they do not establish those separate protocol/UI capabilities.
- Run authenticated Windows model/HUD/browser/resume scenarios in a private
  Windows environment and representative repeated model/harness evaluations.
  CI uses no real credentials. Project-API pilots stay disabled by user choice;
  Opus comparison lacks an authorized account and is not fabricated.

No claim of complete future-plugin compatibility or Opus quality parity is made.

## Optional native pack, 2026-10-31

The [native MCP pack](NATIVE_TOOLS.md) is implemented and installed locally in a
hash-locked private environment. It adds live-catalog image generation/editing,
local PDF extraction, Realtime media exchange and explicitly selected-window
desktop tools. Real Claude completed the media workflow and real Terra completed
an ordinary desktop fixture. Native macOS and Windows GUI results verify actual
state, not merely HTTP status or mocked tool calls. The detailed record retains
failed trials, provider dimension mismatches and the subsequent fixes.

Python isolated startup, same-workspace artifact reuse after resume, cross-project
artifact denial, one-use desktop observations, uncertain-action non-replay,
Unicode focus changes and screenshot/text size bounds have regression coverage.
Generated screenshots no longer appear as both PNG content and base64 JSON text.
The current launcher defaults nonessential Claude telemetry off with the
documented host setting; user overrides remain possible. WebFetch safety checks
and user-requested plugin/tool networking remain separate.

Native Realtime speech worked with existing authorization and no project key.
The first native GPT-Live WebRTC request returned 400; a source-matched V3
request later created a call with 201 but did not establish media, and its
sideband returned 404. TCP connection success, WebSocket upgrade and call
creation are not counted as audio success. [Current detail](NATIVE_TOOLS.md).
The legacy compact endpoint returned 404. The subsequent
[V2 implementation](NATIVE_COMPACTION.md) uses the ordinary
Responses stream and passed actual Claude compact/resume tests; the old 404
did not establish that native compaction was unavailable. The new mode is
opt-in and leaves normal task effort unchanged. No active user session,
original origin, ingress or unrelated
service was restarted for this pack. Read-only production diagnostics still
contain upstream TLS/EOF and slow-compaction incidents; these new capability
checks do not resolve their root cause or certify Opus parity.

The [full Windows Unicode gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41099866806)
subsequently passed: coordinate input, scoped select-all, emoji insertion and
one actual button effect. OSV then identified the initial Pillow pin as affected
by upstream advisories; the capability lock was upgraded to 13.0.0 and a public-
lockfile advisory gate added. All 33 pins returned no matching advisories after
the update. Old immutable environments are not rewritten under live processes.
