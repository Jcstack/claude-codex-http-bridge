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

The current translator does not yet implement that toolset contract. A local
[synthetic translator fixture](tests/native_toolset_contract_fixture.go)
observed empty-name function declarations rather than expanded browser and
computer members, omitted `toolset_name` in replayed and returned calls, and
dropped a `browser_state` block when a text result was also present. A separate
[loopback fake-Responses fixture](tests/probe_toolset_http.py) observed the
same request and streaming/nonstreaming response boundaries through both the
locally selected Mac binary and the uninstalled caller-only candidate. This does not
establish that the current Claude Code client sends those API toolsets, nor
that a real OpenAI backend would accept the synthetic request. Existing browser
MCP behavior is a separate path. Do not treat these client toolsets as supported
until member schemas, collision-safe identity, configuration, result types,
ordering, permissions and real-client/provider behavior are validated.

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
