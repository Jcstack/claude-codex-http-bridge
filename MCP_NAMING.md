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

In the observed collision cases, each synthetic model request contained only
one definition for the shared alias. The HTTP translator therefore could not
detect this particular conflict by looking for duplicate definitions in that
request; the two original identities were already absent at that boundary.
This observation is specific to the tested client and fixture.

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
observed `tools/list_changed`, fetched a changed list, and called the new
original tool in the real client. The earlier version of that dynamic test
allowed the fake model to request the new name before it appeared in that
model request, so earlier passes alone do not prove realistic discovery. A
separate synthetic gateway test exercised
the reviewed translator, deferred discovery through ToolSearch,
both tool calls, and preservation of the cache key, initial tool definitions,
instructions, and previous-input prefix. It did not test a provider cache hit.
The [three-platform source/client gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41407283365)
and [three-platform candidate translator gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41407306970)
passed for these synthetic cases. They do not test real plugins, a real
Windows account, or production transport behavior.

The same test-only adapter later failed once in the [macOS client job](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41444475594/job/125274061156)
without a recorded case or failure category; its source was unchanged from an
earlier passing run. A separate local run passed. Neither observation identifies
the cause. Three further consecutive local macOS runs passed the resume, deny,
and dynamic-list controls; the later [source gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41455750069)
also passed. These samples do not explain or rule out the original intermittent
failure. A later [macOS client failure](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41473378959)
recorded a changed server list without the new alias in the observed requests.
Its precise cause is unproven. In the old test the fake gateway captured the
next request before waiting for the changed-list marker, so that wait could
not update the request's tool list. Also, the fake model emitted the new tool
name even when it was absent from that particular request, and the assertion
only checked whether the name appeared in *any* request.

An isolated, deliberately delayed list response reproduced a stale immediate
follow-up request with the locked client. The updated test keeps the same
client process, lets a synthetic model call the new alias only when that
request actually offers it, and uses bounded later user turns to check eventual
discovery and the original tool call. It records the immediate observation
separately; a later successful turn does not establish instantaneous refresh,
successful execution of stale removed tools, or a fix for the original CI
failure. The adapter's forwarded marker means the list was written to Claude,
not that Claude acknowledged applying it. Only synthetic booleans are emitted;
client output, request bodies, paths and tool arguments remain private. The
candidate remains experimental and is not installed in user sessions.

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

The existing hash-locked capability environment also includes the official
Python MCP SDK. In a local synthetic check of SDK 2.3.0, ordinary tool-call
parameters survived its generic JSON-RPC parse/serialize step, but an unknown
top-level extension field was dropped. With a handler attached to the SDK's
stdio parser logger, one deliberately malformed synthetic message appeared in
the captured error log. This does not establish that valid messages are logged
or that every SDK configuration behaves the same way. It does mean a proposed
SDK-based transport must separately verify future extension preservation and
ensure parse diagnostics cannot disclose message contents. The probe records
only version and boolean results; it starts no server and keeps its synthetic
message and captured log in memory. The desktop adapter now filters this
specific SDK logger before starting its MCP client; the same synthetic parse
error retains a generic diagnostic without its contents. This filter does not
cover other loggers, the desktop child's stderr, or already running processes.
New processes receive it only after an updated capability snapshot is selected.

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
