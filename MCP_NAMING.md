# MCP names and permission identity

An isolated test of Claude Code 2.1.316 found an MCP name collision before the
HTTP bridge: a single synthetic server exposes the distinct MCP tool names
`lookup.fixture` and `lookup_fixture`. Claude presents both under the same
`mcp__gateway__lookup_fixture` alias. When the fake model calls that alias,
reversing the server's tool-list order changes which original tool executes.
The test uses a real client and a direct fake Messages endpoint; no translator,
account, user configuration or actual plugin is involved. A separate local
bridge-path fixture observed the same pattern. These are version-specific
observations, not evidence that the user's plugins contain such a pair.

The [MCP specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools#tool-names)
recommends allowing dots in tool names. Claude's documented
[MCP naming and permissions](https://code.claude.com/docs/en/agent-sdk/mcp#tool-naming-convention)
use the normalized full name for rules, and the
[plugin documentation](https://code.claude.com/docs/en/mcp#use-mcp-servers-from-plugins)
describes character replacement in plugin tool names. The public
[Claude Messages tool-name schema](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)
also uses a narrower name character set than MCP. The bridge cannot infer the
original identity once Claude has merged it into one alias.

In the same synthetic client, allowing the normalized alias while adding a
literal dotted-name deny still allowed the dotted original to be called; the
documented normalized alias, when denied, blocked the call. This demonstrates
why raw MCP names are not sufficient as Claude permission rules. It does not
prove a bypass of a correctly specified normalized deny rule, and the test did
not inspect or change any user's permissions.

Until a safe pre-client solution exists, avoid registering names that become
identical after Claude's normalization. Renaming a server or tool changes its
Claude-facing permission name and may affect other clients; review those rules
before making a change. A deny of the shared normalized alias covers the alias
as a whole in this test, so it cannot selectively allow just one original.
Existing sessions, plugins and MCP servers have not been modified or scanned.

## Experimental pre-client policy

The source tree now contains an **uninstalled experiment**,
`mcp_alias_policy.py`. It is a name and request policy, not a complete MCP
transport. Do not put the test fixture in a real MCP configuration. The
experiment does not start, wrap, or modify any existing server or session.

Within one effective server namespace it preserves ordinary safe names, and
maps names requiring changes to deterministic, reversible ASCII names. For
example, `lookup.fixture` becomes `zZ0_lookup_dfixture`, while
`lookup_fixture` remains unchanged. Original names beginning with the reserved
`zZ0_` prefix are escaped too. The mapping uses no truncation or hash and
rejects a full Claude tool name over 128 characters. It preserves tool order
and the remaining definition fields in the tested messages. This is a
deliberate fail-closed limit, not support for every valid MCP name.

The policy only forwards calls for currently advertised aliases and translates
the selected alias back to the original tool name. A fresh list or
`notifications/tools/list_changed` invalidates the previous list; stale list
replies and calls to unadvertised tools are rejected. Cancellation is forwarded,
and a late reply to a cancelled request is discarded. Cancelled request IDs
cannot be reused during the policy's lifetime; tracking is bounded and fails
closed on exhaustion. Cancellation does not undo a tool effect already begun.
Unit tests exercise paginated lists, overlapping refreshes, cancellation,
server-originated requests in older MCP versions, non-tool messages, and
preservation of synthetic 2026 request metadata and input responses. These
tests do not establish complete support for either protocol version.

With the locked real Claude client, a **test-only** stdio adapter, and synthetic
local servers, both formerly colliding tools were called exactly once each;
the same held after resuming the synthetic session. A deny for the encoded
alias blocked that tool while allowing the other. A separate synthetic test
also observed `tools/list_changed`, fetched a changed list, and called the new
original tool in the real client. A separate synthetic gateway test exercised
the reviewed translator, deferred discovery through ToolSearch,
both tool calls, and preservation of the cache key, initial tool definitions,
instructions, and previous-input prefix. It did not test a provider cache hit.

The remaining work includes production-grade framing, bounded backpressure,
process lifecycle, cancellation races, dynamic server behavior, ToolSearch,
real plugins, MCP protocol changes, and Windows. The test adapter does not
implement these transport requirements. Server namespaces and complete
`mcp__<server>__<tool>` names can also collide *across* servers, including
through separator placement; the per-server mapping cannot resolve those
collisions. Permission rules, hooks, prompts, and skills that refer to a
renamed tool may require explicit review and migration. Broad existing
permissions may still apply. There is no automatic permission migration, and
an already-open session is not migrated. Servers that change tools without a
notification or repurpose an unchanged tool name cannot be made trustworthy
by aliasing alone.

The [2025 MCP stdio specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
uses newline-delimited JSON-RPC and supports server-originated requests. The
[2026 protocol](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)
changes the request and subscription model; future compatibility must be
tested against the actual negotiated versions rather than assumed from a
passthrough fixture.

### Community options assessed

[Supergateway](https://github.com/supercorp-ai/supergateway) is actively
maintained and can prefix tool names or translate transports. Applying the
same prefix to `lookup.fixture` and `lookup_fixture` still leaves them
identical after Claude's normalization, so that option alone does not resolve
this case. Its [non-loopback/API-key warning issue](https://github.com/supercorp-ai/supergateway/issues/360)
is relevant if deploying its server modes; this project did not install it.

[FastMCP's ToolTransform](https://github.com/PrefectHQ/fastmcp/blob/main/docs/servers/transforms/tool-transformation.mdx)
supports renaming tools from proxies and is a serious maintained alternative.
Its open reports include [schema defaults changing across transformed-tool calls](https://github.com/PrefectHQ/fastmcp/issues/6183)
and a [resource-link namespace mismatch](https://github.com/PrefectHQ/fastmcp/issues/4154)
for a specific namespace transform. These reports do not prove that all
FastMCP proxies are affected, but they justify testing schemas, resource links,
dynamic lists, and privacy before adopting a dependency. Neither project has
yet been shown to meet this repository's complete compatibility requirements.

## Offline partial audit

`claude-codex mcp-audit` reads server keys from the default local Claude
configuration and the current directory's `.mcp.json`. It does not execute
commands, contact or initialize MCP servers, or write configuration or audit
files. By default it prints counts only; `--show-names` additionally prints the
conflicting server and tool names locally. `--config FILE` can be repeated to inspect explicitly
selected configuration files instead of the defaults. Server-name collisions
are potential conflicts: they affect calls only where tool names overlap.

To check original tool names too, supply a previously captured local snapshot:

```json
{"servers":[{"name":"gateway","tools":[{"name":"lookup.fixture"},{"name":"lookup_fixture"}]}]}
```

```sh
claude-codex mcp-audit --inventory /private/path/tool-inventory.json --show-names
```

The `name` must be the effective Claude server namespace; plugin scopes must
be supplied in the form Claude uses. Tool descriptions, configuration values,
commands, credentials and endpoints are not printed. The scan does not fetch
or verify the inventory, inspect plugin manifests automatically, enumerate the
tools currently loaded in a running session, resolve configuration precedence,
or cover later tool-list changes. Non-ASCII names and aliases over 128
characters are counted separately. Zero conflicts in a partial snapshot is
not a guarantee of safe or complete plugin behavior. Without a tool inventory,
tool-collision and related unexamined counts are `null`, not zero.

After the [three-platform source and client gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41356275179)
passed on 2026-11-02, the local installation received the reviewed launcher
and audit module. A synthetic installed-command check passed; the selected
account, origin, ingress and current backend retained their identities, and no
service was restarted.

Run the read-only, credential-free client observation after installing the
locked fixture with `npm ci --prefix tests/claude-client --ignore-scripts`:

```sh
CLAUDE_CODEX_TEST_BIN="$(python tests/claude_fixture.py)" python tests/probe_mcp_name_collision.py
```

In PowerShell, preserve any existing fixture setting:

```powershell
$previousFixture = $env:CLAUDE_CODEX_TEST_BIN
try {
  $env:CLAUDE_CODEX_TEST_BIN = (python tests/claude_fixture.py)
  python tests/probe_mcp_name_collision.py
} finally {
  $env:CLAUDE_CODEX_TEST_BIN = $previousFixture
}
```

The default reports the observed categories and verifies its independent
positive and deny controls. `--expect-reproduced` is a version-specific
reproduction check. Neither a negative result nor a passing control proves
that other servers, dynamic tool changes, plugins, permission rules, or future
Claude versions are collision-free. Do not start unknown real MCP servers to
run this fixture.
