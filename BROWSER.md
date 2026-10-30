# Browser tools in the local Claude Code harness

The Codex model can use a browser **through Claude Code's MCP tool system**. Claude Code remains the agent and permission controller. Its built-in `/chrome` integration is [restricted to direct Anthropic subscriptions](https://code.claude.com/docs/en/chrome#prerequisites); this file supplies local alternatives rather than pretending that restriction has disappeared.

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
