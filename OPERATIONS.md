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

## Regression gates

| Probe | Result | Scope |
| --- | --- | --- |
| [Transport](tests/smoke_transport.py) | Auth before upstream; two pre-output 503 retries; truncated output not replayed or declared successful; disconnect cancels upstream; next request works; quota cooldown retained | Fake upstream; pin and candidate passed locally; included in CI |
| [Claude client](tests/probe_claude_client.py) | Compact/resume, changed model/effort on resume, explicit Read hook, denied Write, compaction hints, attribution omission | Real Claude 2.1.314, fake model |
| [Live candidate](tests/live_candidate_subscription.py) | Dynamic reveal, call/result, removal, real Claude ToolSearch then MCP | Isolated macOS subscription flow |
| [Paired translators](tests/live_compare_translators.py) | Two rounds each, all streamed tool calls and continuations passed | Same Sol/high and extra localhost hop; not a coding benchmark |
| [Diagnostics](tests/test_diagnostics.py) | No service startup; bounded parsing; no bodies, paths or credential strings in output | Synthetic privacy regressions |
| [Model state](tests/smoke_model_state.py) | Encrypted reasoning, commentary phase and call/result pairing survive a new proxy process; stable session cache key; no cached-input double counting; max effort, image and JSON-schema translation retained | Fake upstream, candidate passed; this is not a live-stream restart test |

Individual paired requests took 4.258–6.747 seconds for the pin and 3.741–4.970 for the candidate. The small sample found no latency regression; it does not establish a speed advantage.

## Read-only diagnostics

```text
claude-codex diagnostics --hours 2
claude-codex diagnostics --hours 24 --session SESSION_UUID
```

This command makes no network/model call and never starts/stops a service. It reads local logs and recent main-session metadata, then emits an allowlisted JSON report. No prompts, credentials, project paths or raw session IDs are printed; tags are hashes. Error mentions are not retry counts, in-flight requests are absent, and the last main turn's effort does not establish compaction effort.

## Open sessions and service replacement

The current process owns the loopback listener directly. Its [shutdown code](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/sdk/cliproxy/service_lifecycle.go#L256) cancels the runtime context. The existing stop command also removes runtime key/UI files, so it is unsuitable during dependent tasks.

Two isolated persistent-client experiments changed settings while a response remained open. The next request still used the old endpoint, and `/autocompact` retained the old window. Live file edits are not a demonstrated migration method. The host supports plugin reload, but [native translators take precedence](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/sdk/translator/registry.go#L124); a translator plugin cannot simply replace this native route.

A safe upgrade needs a stable authenticated listener, a ready replacement backend, new-request routing and draining of old streams without replay. Local key, session identity, tool history and reasoning state must survive. The [isolated ingress implementation](gateway/README.md) now tests this routing primitive with no external dependencies, including in-flight stream preservation and cancellation. It is not installed; controller integration and initial migration from the existing direct listener remain unresolved. No active process was stopped on the assumption that a quick restart is harmless.

The actual installation uses the Codex backend (`use_siwc=false`, transport errors name `chatgpt.com`), not the public SIWC `api.openai.com/v1` flow. The [SIWC limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) explicitly apply to that public flow. Earlier documents overgeneralized them to every subscription backend. Native hosted tool availability on the current backend needs its own execution tests; it must not be inferred solely from that page. Nor does a Codex desktop feature automatically become a Claude Code tool.

Remaining work: safe handoff/promotion; live reasoning/cache continuity through handoff; diverse repeated repository evaluations; authenticated Windows inference/HUD/browser/resume; Codex-native context retrieval, desktop, steering and media integration. Native `/context`, `/usage` and budget UI still have Claude-alias limitations. API-key pilots stay disabled by user preference and Opus lacks an account. These checks do not certify every feature or Opus parity.
