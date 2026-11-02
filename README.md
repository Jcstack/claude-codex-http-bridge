# Claude Code → Codex HTTP bridge

Claude Code remains the **harness**: it owns the terminal UI, permissions, tools, MCP, skills, hooks, subagents, and conversation. A local authenticated HTTP proxy translates its Anthropic Messages requests to Codex model inference. This project does not run Codex CLI as a tool or agent inside Claude Code.

```text
Claude Code ──Anthropic Messages──> 127.0.0.1 + private local key
                                    CLIProxyAPI ──Codex Responses──> OpenAI
```

The repository contains source and an example config only. It does **not** contain the upstream proxy binary, OAuth credentials, API keys, prompts, transcripts, or runtime logs.

The default installer remains pinned to the reviewed upstream release. A source-locked runtime bundle adds cache-preserving dynamic tools and native search-state replay behind a stable TLS listener. It can be activated without replacing the original service used by existing sessions. CI uses no model credentials, and local live probes use only the existing service's localhost key. See [runtime rollout](RUNTIME.md), the [community review and release gate](COMMUNITY_REVIEW.md), [measured cache and rolling-backend results](OPERATIONS.md), [model-versus-harness evidence](MODEL_HARNESS_EVIDENCE.md), and [local GPT harness pilot](HARNESS_PILOT.md).

## Quick start

Claude Code provides the interface and executes tools; a loopback-only HTTP proxy supplies Codex models. After installation, run:

macOS, Linux, and WSL:

```bash
./install.sh
claude-codex codex-login
claude-codex models
claude-codex usage
claude-codex --model gpt-6.1-sol
```

Native Windows PowerShell:

```powershell
py -3 .\install.py
# Close and reopen the terminal first:
claude-codex codex-login
claude-codex models
claude-codex usage
claude-codex --model gpt-6.1-sol
```

Use `/model` in a session to select a dynamically discovered model and `/effort` to adjust reasoning effort. The local proxy continues running for `/background`; run `claude-codex bridge stop` after background tasks finish. The existing `claude` command retains its original account and settings. If a different `claude-codex` command already exists, the installer refuses before writing the installation; use `./install.sh --replace-command` only if you intend to replace it. An identical managed wrapper can be reused. OAuth files and runtime logs stay in the private local installation and are excluded from Git.

Subagents follow Claude Code's model selection rules by default. Unless configured separately, the `opus`, `sonnet`, `haiku`, and `fable` compatibility aliases all refer to the GPT model selected at startup. A newly launched session can use a different subagent default or alias mapping, for example:

```bash
ANTHROPIC_DEFAULT_SONNET_MODEL=gpt-6.1-sol \
CLAUDE_CODE_SUBAGENT_MODEL=gpt-6.1-terra \
claude-codex --model gpt-6-astra
```

In PowerShell, set `$env:ANTHROPIC_DEFAULT_SONNET_MODEL` and `$env:CLAUDE_CODE_SUBAGENT_MODEL` before running the launch command. Model IDs must appear in the current `claude-codex models` catalog; otherwise the launcher reports an error rather than silently substituting another model. An alias passed to an Agent call or a model in an agent definition can override the subagent default. `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` forces the default according to Claude Code's rules. If the requested alias and main model are recognized as the same family, Claude Code may keep the main model for the subagent even when the alias has a different mapping. A custom subagent definition can specify a full model ID such as `model: gpt-6.1-terra`; the Agent tool's own `model` argument accepts an alias. See the [Claude Code subagent documentation](https://code.claude.com/docs/en/sub-agents) for precedence and exceptions such as forks. New launch settings do not alter open sessions.

After installing a reviewed runtime bundle containing the V2 patch, `claude-codex --native-compaction` opts into native Codex compaction. It preserves the main task's effort and supports continuation after `/compact`; Claude's summary remains the default. Open sessions are not modified. See the [validation results and limitations](NATIVE_COMPACTION.md).

`--resume SESSION_ID`, `-r SESSION_ID`, and `--continue` restore the last GPT model observed in local metadata; an explicit `--model` takes precedence. Models selected later through the HUD are also recorded privately on the machine, without storing prompts or transcript text. Claude Code still handles its native resume picker, and the launcher does not force the default Sol model onto it or guess when names are ambiguous. Model availability is checked against the current account catalog on resume.

For [account switching while sessions run](ACCOUNTS.md), `claude-codex account login work` authorizes and stages an account, `claude-codex account use work` validates and selects it for subsequent requests, and `claude-codex account list` shows the available accounts. The old account remains as a disabled backup and already-started streams can finish; the service, Claude sessions, and native Codex login are not restarted or replaced. Sessions sharing the bridge switch together, and prompt caches may need to warm again. Atomicity between account selection and catalog publication for newly arriving requests remains an open limitation; see [the plan](PLAN.md).

If usage resets but an old 429 persists, the [quota recovery mechanism](QUOTA_RECOVERY.md) checks the selected bridge account's actual availability before clearing an expired local weekly-quota cooldown. The translation layer no longer duplicates the origin's rate-limit state; genuine upstream 429 responses are retained. Run `claude-codex quota recover` for a one-time check without closing sessions, switching accounts, or restarting inference.

The optional [native capability package](NATIVE_TOOLS.md) does not require an additional project API key: run `claude-codex tools install`, then launch normally. It offers image generation/editing, PDF extraction, and Realtime audio through MCP; `--no-native-tools` disables it for one launch. Desktop access requires separate `claude-codex desktop install`, `desktop windows`, and `desktop select WINDOW_ID` commands and authorizes only the selected window for the current project; `desktop disable` revokes that selection. Claude Code continues to control tool permissions. Installation does not grant system permissions or automatically enable the microphone or full-screen capture. These capabilities do not establish complete GPT-Live support, native-compaction coverage, or Opus equivalence.

Optional public API route (pilot; requires a project key and billing review):

```text
claude-codex api key set           # Hidden terminal input; key stays in a private local directory
claude-codex api models            # API-available IDs intersected with official model specifications
claude-codex api --model sol       # Claude Code remains the harness
claude-codex api bridge status
claude-codex api bridge stop       # Stop after API background tasks finish
```

The API route is separate from subscription OAuth and uses separate service ports and quota state. `api key set` does not accept a plaintext key argument. Create a project key in the [OpenAI API Platform](https://platform.openai.com/api-keys) and configure a [hard spending limit](https://developers.openai.com/api/docs/guides/production-best-practices). If you have claimed [ChatGPT subscriber API credits](https://developers.openai.com/api/docs/guides/chatgpt-api-credits), verify the project's organization and balance in the API Platform. Live inference, billing, and the complete tool set still require validation with that project key; this repository's automated tests use a fake upstream only.

## Requirements

- macOS, Linux, or native Windows on ARM64/aarch64 or x64
- On Windows: Windows 10 1809+ or Windows Server 2019+ and an NTFS installation directory. Windows Subsystem for Linux uses the Linux instructions.
- Claude Code installed and on `PATH` (verified with Claude Code 2.1.312–2.1.316)
- Python 3.10+; on macOS/Linux/WSL, `bash` and `lsof`
- A ChatGPT account with Codex model access
- Or, for `claude-codex api`, an OpenAI API project key with access to supported GPT models
- Optional quota fallback: native Codex CLI signed into the selected bridge account; a different selected account uses its fixed ChatGPT usage endpoint

## Install

macOS/Linux/WSL: run `./install.sh`. Native Windows: run `py -3 .\install.py` in PowerShell or `py -3 install.py` in CMD, then open a new terminal before using `claude-codex`. If `py` is unavailable but Python is installed, use `python`.

```text
claude-codex codex-login
claude-codex models
claude-codex usage
```

The installer downloads the pinned [CLIProxyAPI v8.0.46 release](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.46), verifies its published SHA-256 digest, and saves it under `~/.local/share/claude-codex-http-bridge/` on macOS/Linux or `%LOCALAPPDATA%\claude-codex-http-bridge\` on Windows. It generates a random local key and a private config on your own machine. `codex-login` opens the OAuth flow and stores the resulting credential only in the installation's private `auth/` directory. The Windows installer adds its private `bin` directory to the current user's PATH; open a new terminal to use the command.

If a different `claude-codex` command already exists at the install location, a preflight rejects the conflict before writing installation files. Use `--replace-command` only when you want the new wrapper to replace that command. It does not copy or remove any existing OAuth credential. A matching managed wrapper can be reused.

Reinstalling controller source is not yet a multi-file atomic update and has not been proven safe while other launches or sessions depend on the installation. Wait until those sessions and background tasks finish before reinstalling source. The separately reviewed `claude-codex bridge upgrade BUNDLE` can roll the managed backend while admitted streams drain, but it does not update the controller scripts or replace a running ingress.

For an isolated installation, pass `--root /private/path --bin-dir /private/path` to `install.sh` on POSIX, or `--root C:\private\path` to `install.py` on Windows.

## Use

```bash
cd /path/to/your/project
claude-codex                         # newest catalog-listed Sol
claude-codex --model gpt-6-sol       # exact model ID
claude-codex --model gpt-5.6-sol --effort max
claude-codex bridge status            # localhost service state
claude-codex autostart enable         # restore the subscription bridge at login (macOS/Windows)
claude-codex autostart status         # login recovery and last local health result
claude-codex bridge reload            # replace a managed backend and drain old requests
claude-codex bridge rollback          # restore the previously recorded managed runtime
claude-codex diagnostics --hours 2    # read-only cache, latency, failures and compaction
claude-codex diagnostics --lifetime --tracked --include-children # audit a previously recorded private target
claude-codex bridge stop              # after background work has finished
```

Inside Claude Code:

- `/model` lists the eligible text models discovered from the local bridge's `/v1/models` catalog when this session started. Use the arrow keys and press **s** to switch for this session only. Enter saves the model to Claude Code's user settings and may affect a later plain `claude` launch. `claude-codex models` checks the current catalog; an already running session's picker is not automatically refreshed.
- `/effort` selects `low`, `medium`, `high`, `xhigh`, or `max`. `/effort ultracode` enables Claude Code's own dynamic workflows; it is not Codex Ultra.
- `/autocompact` shows the configured window for the selected model. The launcher supplies a per-model setting, so `/model` switches also switch that setting. Claude reserves space internally and may compact earlier. Its higher-priority launch setting cannot currently be changed by `/autocompact` within that session.

The model list, context windows, compaction settings and HUD mapping come from the authenticated catalog at session start. Discovery uses a valid `max_context_window`, preserves `context_window` as the default, and otherwise uses the declared default. Model IDs and context sizes remain dynamic. New sessions refresh these values; running sessions retain their startup settings. The proxy release itself stays pinned until its checksum and compatibility have been reviewed.

With an activated [reviewed runtime bundle](RUNTIME.md), new launches use the stable TLS endpoint automatically. Existing direct sessions retain their original service. `bridge reload` keeps the TLS listener and active model streams; replacing that listener or the legacy origin itself is a separate migration. `diagnostics` reports origin and managed backend logs separately, without reading keys or summing duplicate layer counts.

[Longitudinal diagnostics](SESSION_AUDIT.md) adds explicit lifetime/child-log coverage and stream-failure categories. The [capability plan](PLAN.md) tracks verified work and remaining acceptance gates.

Claude Code streams partial model output through the bridge. See [STREAMING.md](STREAMING.md) for what was measured and how ordinary SSE differs from GPT-6 mid-turn steering and GPT-Live voice.

On 2026-10-30 Astra advertised a **272,000 default and 872,000 maximum**; Sol/Terra 6.1 advertised a 500,000 default and the same maximum. Earlier bridge versions mistakenly used the default as the limit. The corrected code uses the maximum and a 90% auto-compact window (784,800 for these rows), subject to lower native limits and Claude's internal headroom. A live Astra request accepted 280,346 input tokens. See [the correction, cache tests and compaction incident](OPERATIONS.md).

### Claude HUD and other status-line plugins

When user settings are enabled, the launcher reads the existing user `statusLine.command` and passes Claude Code a private per-launch settings file that runs that same command through `statusline_adapter.py`. The current source skips that user fallback when a CLI flag or inherited environment variable selects restricted, safe or bare mode, and when `--setting-sources` excludes user settings; an explicit launch status line is handled separately. At startup, the source also leaves detected or uncertain project/local status-line precedence to Claude and, if a user fallback would otherwise be wrapped, prints a notice that the bridge cannot correct the resulting context display. The local launcher for that last change is tracked separately; existing sessions retain their startup settings. See [SECURITY.md](SECURITY.md). The adapter corrects the context size and percentage for the **current** Codex model, including after `/model` switches. It removes Claude-list-price dollars, Anthropic prompt-cache timers, and Anthropic rate-limit fields because they do not describe this Codex route. It follows the **selected bridge account**, excluding disabled backups. It reads the official `account/rateLimits/read` RPC when native Codex uses that account, or that account's fixed ChatGPT usage endpoint otherwise. It never invents a missing window or displays a cache from another account. A private local cache holds only percentages, reset times, and an account fingerprint for five minutes. Other status-line fields and the user's original command are preserved; the user's settings file is not changed. The per-launch file remains available to detached background sessions and is removed by `claude-codex bridge stop`. Set `CLAUDE_CODEX_STATUSLINE_ADAPTER=0` before launch to disable the adapter. Run `claude-codex usage` to see exactly which windows your account reports. Late changes to project settings and `/cd` remain open compatibility checks.

For status-line commands that understand bridge-specific fields, `claude-codex cache telemetry enable` opts subscription sessions into reported cache counters; `disable` and `status` manage the same private local preference. Existing sessions pick it up at their next status-line refresh. Set `CLAUDE_CODEX_STATUSLINE_CACHE_METRICS=1` or `0` before a launch to override the shared choice for that session. The extension supplies available counters under `claude_codex_bridge.reported_main_cache`: `requests`, `caching_observed`, and `hit_ratio`. The ratio is the fraction of reported main-conversation input tokens read from cache, not a request hit rate. It does not report a cache expiry or prove that a later request will hit. The standard `prompt_cache` object remains omitted because a partial object can make existing plugins claim the cache is cold. See [the cache audit](CACHE_AUDIT.md).

Before shared-preference support, the launcher did not record an explicit `0` in the session profile. The updated adapter can honor the value when Claude passes it to the status-line subprocess; if the environment was stripped, the original choice cannot be recovered from that older profile. Disable the shared preference or start a new session with `0` if needed.

The reviewed local HUD 0.15.0 installation includes a separate [patch](upstream/patches/claude-hud-v0.15-reported-cache.patch) to display this bridge-specific counter. It is a local modification, not an upstream HUD feature. An upstream plugin update can overwrite it and needs a new review; other plugins may ignore the extension. A session can include model or account changes, so the counter is not an account-level measure.

Plain `claude` remains separate and uses its ordinary provider settings. `claude-codex models` is a catalog listing, not an entitlement guarantee; a completed inference request confirms access to a selected model.

The current launcher accepts a per-launch `--settings` file or JSON object and composes it with bridge settings into one private file. For example, run `claude-codex --settings ./session-settings.json`. Custom hooks and model entries are combined with bridge entries; ordinary user and project settings still load through Claude Code. Authentication, provider, TLS and proxy overrides are rejected. A numeric compact window must be within the dynamically prepared window for each affected known model. Per-model `"auto"` is resolved to that model's current bridge window instead of the mapped Claude alias's window; unknown model windows are rejected. The input must be a regular JSON object without duplicate keys; the combined settings cannot exceed 2 MiB. An explicit custom `modelPicker` replaces the generated picker. When an option position is ambiguous, put `--settings` before other options. The original custom file is a startup snapshot, and the private snapshot may remain until bridge-stop cleanup. See [SECURITY.md](SECURITY.md).

`claude-codex api models` first intersects the official API `/v1/models` IDs with the current [official model catalog](https://developers.openai.com/api/docs/models), then reads each matching model's official Markdown specification for context, effort and search support. Only text models with a verified context of at least 200k, `low`–`max` reasoning, and web search enter the Claude Code picker; unknown or unverifiable models are omitted instead of receiving an invented size or unsupported effort. Stop the API bridge after background work and start it again to refresh its upstream model list. The API key is sent only to `https://api.openai.com/v1/models` for discovery and to the pinned local proxy's official OpenAI Responses route for inference; it is never sent to the documentation site.

## Tested behavior and boundaries

On macOS Apple Silicon, the bridge was tested with GPT-6.1 Sol, GPT-6 Sol, GPT-5.6 Sol and Astra inference. In one interactive session, `/model` switched GPT-6.1 Sol → GPT-6 Sol → GPT-6.1 Terra and the actual response model changed. Earlier 400k/200k compaction settings used default context metadata and have been replaced for new launches by 90% of the discovered maximum (currently 784,800 for an 872,000 maximum). An authenticated `--bg` task completed with a `PONG` response after the launcher exited. Claude Code WebSearch, WebFetch, Read, Edit, Bash, Agent, MCP/ToolSearch, image input, structured output, and streaming were exercised during bridge evaluation; this is a functional sample, not exhaustive certification of every Claude Code release and command. Native Windows CI checks installation, file ACLs, model catalog logic, proxy service lifecycle, launcher startup and real-client offline fixtures without model credentials; live Windows inference still requires an authenticated Windows session.

Claude Code 2.1.314 can feed downloaded PDF bytes to WebFetch's summarizer, which may produce an unsupported summary. Bridge launches now add a `PostToolUse:WebFetch` hook that replaces only results carrying Claude Code's binary-PDF marker with a clear warning and the verified saved-file path. The model can then use Claude Code `Read` or local PDF extraction to inspect the actual document. The hook makes no extra network request or file copy, works inside subagents, and does not change the user's settings file or other WebFetch results. Set `CLAUDE_CODEX_PDF_GUARD=0` before launching to opt out. [Upstream Claude Code issue](https://github.com/anthropics/claude-code/issues/90416).

`claude-codex mcp-audit` is an offline, partial check for MCP names that collide after Claude's normalization. It does not connect to MCP servers or inspect live sessions; an optional local tool-list snapshot extends its scope. See [MCP naming and permission limits](MCP_NAMING.md).
Claude Code itself still saves the downloaded binary; this hook does not change its [file retention behavior](https://github.com/anthropics/claude-code/issues/104178).

`modelPicker.behavesAs` lets Claude Code offer model capabilities and avoids its `unrecognized_model` diagnostic. Claude Code may therefore display the mapped Claude model's **1M context and list-price cost**. Those UI figures are not the Codex subscription route's limit or bill. The selected maximum from the local catalog controls the generated auto-compaction window. See [Claude Code model configuration](https://code.claude.com/docs/en/model-config) and [the per-model settings reference](https://code.claude.com/docs/en/settings-reference#modelsettings).

The reviewed managed runtime also preserves native hosted-search state and URL citations across translation and resume. It sends incremental citation metadata alongside unchanged answer text; exact source excerpts remain unavailable from OpenAI URL annotations. Streaming, non-streaming, real Claude resume, and a live subscription search across a managed backend reload passed. [Runtime behavior](RUNTIME.md), [measurements and boundaries](OPERATIONS.md).

Fine-grained tool-input streaming is enabled by default on the **subscription** route. Set `CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING=0` before launching if a future Claude Code or proxy version regresses. The separate API route leaves it off because the pinned translator forwards Claude's `eager_input_streaming` field to OpenAI Responses instead of removing it. A live Sol tool call and an isolated Responses translation test are recorded in [STREAMING.md](STREAMING.md).

The localhost proxy starts on first use and is reused by later `claude-codex` sessions, including detached `/background` and `--bg` work. Run `claude-codex bridge status` to inspect it, and `claude-codex bridge stop` after background work has finished; stopping it disconnects any still-running sessions. Claude-account features such as Remote Control have [separate provider requirements](https://code.claude.com/docs/en/remote-control#requirements). OpenAI's [public SIWC flow](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) has tool restrictions, while this installation uses the Codex backend. Its model capabilities still require explicit Claude-side tool integration and route-specific execution tests; see [the audit correction](OPERATIONS.md).

On macOS and Windows, `claude-codex autostart enable` installs a current-user login check. It retries an unavailable bridge every two minutes and checks healthy services using local metadata only. A successful manual `bridge stop` pauses recovery until `bridge start` or a new session starts the service again; `autostart disable` removes the login check without stopping running sessions. With a reviewed managed runtime, recovery reuses its saved TLS port, CA, and client key. If that port belongs to another process or the private identity is missing, recovery fails closed; it does not kill the other process or create a different identity. A process or computer restart interrupts active streams, and recovery does not replay them. Login recovery for Linux is not yet provided; `bridge start` and ordinary launches can restore a dead managed endpoint on all supported platforms. Windows login and real-account behavior still require testing on a user's Windows installation.

The macOS login environment does not normally inherit a terminal's proxy variables. When enabling recovery from a shell with explicit HTTP/HTTPS proxies that match macOS system settings, the bridge records only the selected schemes and that shell's bypass rules in its private runtime directory. At login it reads the current system proxy addresses in memory and refuses to start the upstream process if an expected proxy is missing or its loopback listener is not ready. A loopback readiness check opens a TCP connection without sending credentials or an inference request; it does not prove Internet access. Proxy URLs and credentials are not copied to the login entry or that policy file. A shell proxy that differs from system settings, automatic proxy configuration/discovery, and unsupported system bypass rules require separate configuration; they are not silently replaced with a direct connection. Re-run `autostart enable` after intentionally changing the shell's bypass rules. On Windows, automatic recovery of system proxy settings has not been verified; explicit login-environment proxy variables and manual starts should be checked separately.

If `autostart status` reports `network_configuration`, check that the expected macOS system proxy is available and rerun `autostart enable` from the terminal after an intentional configuration change. The status records only this fixed category, not a proxy URL, credential, or error text.

If Claude Code reports `503 Connection Closed` and names a localhost inference port, compare that port with `claude-codex bridge status`. When the reported port is no longer listening but the bridge status names a healthy different port, leave the current service running and reopen the old conversation from its original directory with `claude-codex --resume SESSION_ID`. This starts Claude Code with the current private endpoint. A catalog check with `claude-codex models` confirms the local HTTP service; a small inference request is needed to confirm the model route itself. Do not restart unrelated application services to fix a stale Claude Code endpoint.

A live port can also return errors when its upstream connection fails. CLIProxyAPI's default transient-error cooldown is about 60 seconds: one upstream TLS handshake failure or HTTP 520 can otherwise make later requests to that model fail fast with local 503. New bridge sessions allow two additional retries for retryable transport failures and selected 5xx statuses, and disable that transient-error cooldown. HTTP 520 can still surface once, and quota/429 cooldowns remain active. Older installations without a custom `routing` section receive these settings when creating a new private session; an explicitly customized routing section remains under the operator's control.

On macOS/Linux/WSL, the launcher replaces its Python process with the Claude Code executable after preparing the local service and session settings. In tmux, the command wrapper labels only its own window `claude-codex` while it runs and restores that window's previous automatic-renaming behavior on exit; it does not edit the global tmux configuration. Already-running sessions keep their existing window title until restarted. Native Windows keeps its existing subprocess launch path.

## Security and privacy

- The proxy binds `127.0.0.1` and requires a random local key stored in the private runtime directory for the service lifetime. The controller checks the listener PID and process creation identity before reusing a service. A legacy state is adopted only after verifying its executable; the process identity is then stored as a hash in private state. On Windows listener ownership uses `netstat`; on POSIX it uses `lsof`. The ingress's individual origin requests currently check PID and port, not process birth time; see [SECURITY.md](SECURITY.md).
- On POSIX, OAuth and runtime directories are `0700`; credentials, config, key, and checksum files are `0600`. On Windows, NTFS ACLs restrict installation and runtime files to the current user and are verified before launch. The proxy binary is checked against the installation's verified release hash at each launch.
- Proxy debug logging, request logging, usage statistics, the management panel, and the auto-update panel are disabled. Claude Code itself can still save local transcripts and communicate with services required by its own features.
- The `.gitignore` is a source allowlist. Before pushing changes, inspect staged files and run `gitleaks git --redact .`.

See [SECURITY.md](SECURITY.md) for credential-handling rules.

See [COMPATIBILITY.md](COMPATIBILITY.md) for the Claude Code feature matrix, Claude HUD behavior, current route limits, and a source-backed model comparison.

See [CAPABILITY_AUDIT.md](CAPABILITY_AUDIT.md) for the broader local-harness, model-route, provider, plugin, Windows, and community-project audit. [BROWSER.md](BROWSER.md) gives an opt-in browser MCP setup, including an isolated mode that was exercised with a real Codex-backed Claude Code session.

See [API_ROUTE_RESEARCH.md](API_ROUTE_RESEARCH.md) for the direct OpenAI API-key route, current independent Opus/Fable/Sol/Astra comparisons, active gateway issues, privacy requirements, and a credential-free Responses protocol test. The API command is an opt-in pilot; the plain `claude-codex` command still uses the ChatGPT-plan route.

## License

The wrapper and installer are MIT licensed. CLIProxyAPI is downloaded separately under its own MIT license; its license is retained in the local installation.
