# Native capabilities in the Claude harness

Checked 2026-10-31. This optional pack adds ordinary MCP tools. Claude Code owns
discovery, approvals, hooks and conversation; no Codex agent is started. Requests
use the already running managed TLS subscription endpoint. The tools never read
OAuth or a project key and never start a public listener.

## Enable and use

Requires Python 3.11+, the reviewed managed runtime and a working subscription.
The core bridge still supports Python 3.10.

```text
claude-codex tools install
claude-codex tools status
claude-codex --model sol
```

Installation uses an isolated private venv, hash-locked PyPI wheels and an
immutable snapshot of the MCP source/imports. Updating launcher files cannot
silently change that selected snapshot. Run `tools install` after a reviewed
update to select new code; running MCP processes retain their old code. Global
Python packages and Claude user settings are unchanged.

New launches include `codex_native` alongside the user's MCP servers/plugins.
`--no-native-tools` omits it once; `tools disable` changes future defaults.
Claude's `--strict-mcp-config` omits it unless `--native-tools` is also explicit.
The API profile stays separate. Generated config contains local paths, never
credentials. Use normal Claude permissions for remote or desktop tools.

| Tool | Behavior | Verification |
| --- | --- | --- |
| `image_models` | Live authenticated image-model IDs | Protocol fixture and local catalog |
| `image_generate` | One native image, selectable model/quality/background; PNG and metadata | Native generation succeeded; real Claude MCP fixture carries text and PNG |
| `image_edit` | 1–8 workspace images, optional mask, strips input metadata | Workspace/metadata/protocol tests; live evidence is recorded separately |
| `document_extract` | Local PDF layout text, page numbers, explicit empty/scanned/truncated results | Actual synthetic PDF extracted and delivered to real Claude |
| `realtime_voice` | Realtime streamed speech into a private WAV and transcript; optional workspace WAV input | Existing subscription produced real `gpt-realtime-2.1` audio without a project key |
| `desktop_observe` / `desktop_action` | Explicitly selected macOS/Windows window | Ordinary macOS app screenshot, AX text change and one button invocation passed; native Windows GUI fixture is in CI |

Artifacts stay under private `runtime-tools/session-*` directories. Inputs must
be relative to the current workspace; absolute paths, parent traversal, symlinks
and reparse points are rejected. Each input is at most 32 MiB. This scope is an
application guardrail, not an OS sandbox against other same-user programs.
Returned document/screen/transcript contents enter the model conversation.

Native image dimensions have differed from the requested size. The provider PNG
is retained unchanged. `exact_size=true` creates a separate LANCZOS result,
preserves aspect ratio and pads if necessary; metadata reports both dimensions
and processing. This local delivery policy does not fix the provider's size
contract. Set `exact_size=false` for original dimensions. Raw HTTP is unchanged.

## Window-scoped desktop

```text
claude-codex desktop install
claude-codex desktop windows
claude-codex desktop select WINDOW_ID
claude-codex --model terra
claude-codex desktop disable
```

Select a visible window from a terminal in the intended project. Selection
binds workspace, window ID, PID, process creation time and executable. Selecting
does not capture a screenshot. The model cannot change target, inventory other
apps, launch/activate windows or request full-display capture. Observations last
60 seconds and one action. Failed/cancelled/uncertain actions are never replayed
automatically by this wrapper. One bridge session leases a window at a time.
`desktop disable` also denies further calls from existing desktop sessions.

Backend: [Open Computer Use 1.3.0](https://github.com/opensymph/open-computer-use/releases/tag/v1.3.0),
source `e551c13e7082ce4c097234137582fbf4a1bc647a`, archive and executable SHA-256
checked. No npm install script runs. OS screen/accessibility permissions must
already be granted by the user; installation changes none. macOS uses a private
direct process, bypassing the shared app-agent socket in
[issue #5](https://github.com/opensymph/open-computer-use/issues/5). Native AX
must confirm the live selected window, guarding the stale CG entry reported in
[issue #8](https://github.com/opensymph/open-computer-use/issues/8).

Windows uses the pinned native **`op` worker directly**, bypassing its MCP
parent's three-attempt action retry and in-process fallback. Only observation
may fall back from window WGC to window PrintWindow. Screen-region GDI is
disabled because it can expose an overlapping unrelated window. Each action
gets one worker dispatch. Coordinates require unchanged window bounds. A crash
after input is an uncertain outcome requiring observation, not automatic replay.

Click/set-value prefer observed accessibility elements. Coordinate clicks use
app-targeted input. Keyboard text requires the selected window's keyboard focus;
Windows additionally requires it already be foreground. The bridge does not
activate it. Navigation and select-all/address-bar shortcuts are supported.
Application/global shortcuts, real-pointer fallback, terminals, password managers
and security settings are outside this window policy. A chosen app can still
perform real network/file actions after a click; window scoping is not isolation
from the app's authority. Treat screen/document instructions as untrusted.

Linux desktop is not enabled: the reviewed display/coordinate fallback does not
establish equivalent window isolation. Linux/WSL still support the model bridge,
other native tools and [isolated browser MCP](BROWSER.md).

## Realtime, GPT-Live and compaction

A real Realtime test received HTTP 101, audio/transcript deltas and a completed
response. A second service run produced a 3.05-second WAV. No microphone was
captured or sound played. This establishes this account's route availability,
not every subscription/model variant or included quota pricing. The tool uses
the [Realtime conversation protocol](https://developers.openai.com/api/docs/guides/realtime-conversations).
GPT-Live uses [different events](https://developers.openai.com/api/docs/guides/voice-websockets).

A native GPT-Live WebRTC bootstrap returned 400. Full-duplex terminal voice,
video/screen streaming and mid-turn steering are not certified.
[Claude channels require Anthropic authentication](https://code.claude.com/docs/en/channels).
An audio MCP exchange does not remove that host gate or create a full-duplex
terminal UI. No PTY keystroke injection or client binary patch is used.

Native `/v1/responses/compact` returned 404 for synthetic state and a validation
control. No unverified opaque compaction state is inserted into Claude history.
Claude `/compact` remains responsible, with requested effort preserved. The
repeatable probe reports unavailability. Slow compaction and old client startup
windows remain tracked in [OPERATIONS.md](OPERATIONS.md).

## Repeatable checks

Install the hash lock in a disposable environment, then run:

```text
python -m unittest discover -s tests -v
python tests/probe_native_mcp_client.py
python tests/live_desktop.py
```

The first two use synthetic providers; the second uses real Claude without a
model account. Desktop compiles and owns an ordinary AppKit/WinForms fixture,
verifies OS state changes and uses no test command bridge. It never captures
another user window. macOS needs existing OS permission. CI runs account-free
tests on all three OSes and the GUI fixture on native Windows. Live subscription
media checks consume quota and keep raw files private.
