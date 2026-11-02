# Browser tools in the local Claude Code harness

The Codex model can use a browser **through Claude Code's MCP tool system**. Claude Code remains the agent and permission controller. Its built-in `/chrome` integration is [restricted to direct Anthropic subscriptions](https://code.claude.com/docs/en/chrome#prerequisites); this file supplies local alternatives rather than pretending that restriction has disappeared.

## Anthropic client toolsets

Anthropic's Messages API separately defines [browser](https://platform.claude.com/docs/en/agents-and-tools/tool-use/browser-use-tool)
and [computer](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)
client toolsets. Their dated declarations have no `name`; the client runs
member tools, distinguishes them using `toolset_name`, and preserves typed
results such as `browser_state`. Browser batches have an ordered execution
and failure contract. These API toolsets are distinct from `/chrome` and from
the Playwright MCP option below.

For the dated `browser_toolset_20260801` contract, Anthropic documents 27
default members and four optional members that default to disabled; the dated
computer toolset has 17 members. The browser documentation states that input
bounds must be enforced by the executor, and describes a classifier on
Anthropic's own browser-tool service path. Neither that classifier nor
equivalent protection has been established for this Codex bridge. These counts
and defaults describe the cited toolset versions, not a dynamic catalog or a
promise about later versions.

The official Python SDK's [version 2.4.0 source](https://github.com/anthropics/anthropic-sdk-python/tree/5752c132041af71055584632caab5ca2325cae5e)
contains generated member input models and discriminated response unions. The
[static inventory](tests/audit_toolset_sdk.py) reads that source without
importing or executing it: it found input classes for all 31 browser and 17
computer members, with 16 names shared across the families. Each shared name
has a different set of top-level input field names in these generated types.
It also confirms that upload, JavaScript, console and network readers default
to disabled in that snapshot. The member inventory and negative controls
passed on [macOS, Linux and Windows](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41530519329).
The audit does not verify the checkout's integrity itself: fetch
the public commit and verify its SHA and clean state independently, as the
[isolated workflow](.github/workflows/toolset-sdk-inventory.yml) does. It is
not a schema exporter, tool executor, or permission system.

Those generated input types are useful references but are not proven identical
to the model-visible schemas or sufficient for safe execution. For example,
the pinned browser models annotate `scroll_amount` as `Optional[int]` and
`wait.duration` as `float`, while their prose describes bounds. The official
[SDK guide](https://github.com/anthropics/anthropic-sdk-python/blob/5752c132041af71055584632caab5ca2325cae5e/browser-toolset.md)
says that the driver must check values; it supplies neither a browser driver
nor a URL policy by default, and exception text can be returned to the model.
Any eventual executor needs its own scoped permissions, value checks and safe
errors. Browser member input is documented as arriving in one complete
`input_json_delta`; ordered actions stop after the first failure or refusal,
while other tools can still run. Earlier synthetic fixtures do not establish
those behaviors for this bridge.

A separate [request-local naming fixture](tests/toolset_identity_fixture.py)
experiments with distinct, deterministic aliases for browser and computer
members. It rejects collisions with custom names, unknown or removed returned
tools and unsupported names, while retaining known historical names for replay.
Local controls cover the SDK snapshot's 48 members, a same-name custom tool,
dynamic additions and separate requests. This is an isolated mapping experiment:
it does not provide schemas, authenticate a session, process real streams,
validate permissions or expand toolsets. Its collision and length rejections
are compatibility limitations, so it is not included in the runtime. Its
64-character ASCII function-name assumption follows the public
[OpenAI SDK definition](https://github.com/openai/openai-python/blob/main/src/openai/types/shared_params/function_definition.py),
not a verified constraint of the Codex subscription backend.

The current translator does not yet implement that toolset contract. A local
[synthetic translator fixture](tests/native_toolset_contract_fixture.go)
observed empty-name function declarations rather than expanded browser and
computer members, omitted `toolset_name` in replayed and returned calls, and
dropped a `browser_state` block when a text result was also present. A separate
[loopback fake-Responses fixture](tests/probe_toolset_http.py) observed the
same request and streaming/nonstreaming response boundaries through both the
locally selected Mac binary and the uninstalled caller-only candidate. When
the synthetic result contained only `browser_state`, its JSON arrived as a
string rather than the original typed block. The mixed-result, state-only and
tool-identity observations also passed on [three platforms](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41529298769).
This does not
establish that the current Claude Code client sends those API toolsets, nor
that a real OpenAI backend would accept the synthetic request. Existing browser
MCP behavior is a separate path. Do not treat these client toolsets as supported
until member schemas, collision-safe identity, configuration, result types,
ordering, permissions and real-client/provider behavior are validated.

An uninstalled [browser-state candidate](upstream/patches/browser-state-candidate.patch)
preserves a `browser` tool result's `browser_state` JSON as an ordered Responses
`input_text` item, both beside text/images and when it is the only result.
Synthetic HTTP and native-executor tests passed for streaming and nonstreaming
requests on [three platforms](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41529298769);
the preceding source failed the corresponding native controls. A synthetic
comparison without browser-state content kept the same prompt fields and
session cache key, with and without an explicit direct caller policy. The
candidate combined with the separate caller-only experiment also passed a
temporary managed TLS handoff and locked-client continuation on those runners.
The handoff fixture mocked the bundle source lock and did not send a
browser-state result during the transition. None of these checks exercised a
real provider cache.

This is not a typed Responses `browser_state` block or complete toolset support:
member expansion and `toolset_name` identity remain unresolved, and no browser,
real provider or real tool was used. Unlike the current mixed-result behavior,
the candidate would send the complete state JSON to the model, including any
tab titles, URLs and download fields present. That intentionally changes the
prompt for such results and can affect cache reuse; it needs explicit privacy,
size, real-client and provider validation before rollout.

## Isolated browser (recommended first)

Install Node.js LTS, then create a JSON file outside the public repository with:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp@0.0.83", "--isolated", "--no-webmcp"]
    }
  }
}
```

Start Claude Code through the model bridge:

```bash
claude-codex --mcp-config /absolute/path/browser-mcp.json
```

On native Windows, use a Windows path for `--mcp-config`; if your MCP client cannot resolve `npx`, set `command` to the absolute path of `npx.cmd`. This is a per-session MCP addition. Do not pass `--strict-mcp-config` for normal use, because that flag intentionally hides your other configured MCP servers. The bridge forwards Claude Code flags and leaves your existing hooks, plugins, skills, and MCP settings active.

The pinned [Playwright MCP](https://github.com/microsoft/playwright-mcp) uses an in-memory profile with `--isolated`, so it does not reuse your everyday browser's cookies. `--no-webmcp` keeps page-supplied WebMCP tools out of the agent tool list. A live test on this bridge used the GPT-6.1 Sol model to call `ToolSearch` and `mcp__playwright__browser_navigate`, then read the title of `https://example.com`. Browser package and Windows live behavior still need separate checks on your Windows machine.

Browser snapshots, page text, and screenshots returned by an MCP tool become conversation content sent to the Codex model route. An isolated profile limits which local login state the tool can see; it does not keep the visited page content out of model requests.

## Existing signed-in Chrome (opt-in)

If a task specifically needs the browser you are already signed into, [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) can use Chrome 144+'s auto-connect. Enable remote debugging manually in `chrome://inspect/#remote-debugging`, then use this per-session MCP entry:

```json
{
  "mcpServers": {
    "chrome-devtools": {
      "command": "npx",
      "args": ["-y", "chrome-devtools-mcp@1.12.0", "--autoConnect", "--no-usage-statistics", "--no-performance-crux"]
    }
  }
}
```

Google's [auto-connect privacy guide](https://developer.chrome.com/docs/devtools/agents/use-cases/auto-connect) says the attached agent can access the real profile's tabs, cookies, and storage. Use a dedicated Chrome profile when possible, review browser permission prompts, and keep Claude Code's ordinary permission checks on. The telemetry opt-out flags above disable Chrome DevTools MCP usage statistics and CrUX lookups; they do not change the sites you visit or Claude Code's own network traffic. This mode was reviewed from source documentation but **not** enabled in the user's real browser during this audit.

Neither browser MCP option grants arbitrary desktop control. The optional [native pack](NATIVE_TOOLS.md) supplies a reviewed, explicitly selected macOS/Windows window with OS permissions, one-use observations and no global-pointer fallback. Linux desktop fallback remains disabled.
