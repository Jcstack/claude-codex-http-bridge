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
Python packages and Claude user settings are unchanged. The server runs Python
with `-I` and imports only its reviewed snapshot and locked environment; a
workspace `PYTHONPATH`/`sitecustomize.py` cannot enter its startup path.

New launches include `codex_native` alongside the user's MCP servers/plugins.
`--no-native-tools` omits it once; `tools disable` changes future defaults.
Claude's `--strict-mcp-config` omits it unless `--native-tools` is also explicit.
The API profile stays separate. Generated config contains local paths, never
credentials. Use normal Claude permissions for remote or desktop tools.

| Tool | Behavior | Verification |
| --- | --- | --- |
| `image_models` | Live authenticated image-model IDs | Protocol fixture and local catalog |
| `image_generate` | One native image, selectable model/quality/background; PNG and metadata | Real subscribed Claude generated an image through ToolSearch; text and PNG delivered |
| `image_edit` | 1–8 workspace images, optional mask, strips input metadata | Real subscribed Claude edited its generated image; workspace/metadata/ownership regressions passed |
| `document_extract` | Local PDF layout text, page numbers, explicit empty/scanned/truncated results | Actual synthetic PDF extracted and delivered to real Claude |
| `realtime_voice` | Realtime streamed speech into a private WAV and transcript; optional workspace WAV input | Existing subscription produced real `gpt-realtime-2.1` audio without a project key |
| `desktop_observe` / `desktop_action` | Explicitly selected macOS/Windows window | Real Terra + Claude drove an owned macOS window; native Windows screenshot, text, button and Unicode fixture passed |

Artifacts stay under private `runtime-tools/session-*` directories. Inputs must
be workspace-relative or a verified generated artifact owned by this workspace.
Private sidecars bind artifact name, SHA-256 and a hashed workspace identity, so
a resumed process can reuse media from the same project. Another workspace is
denied. Other absolute paths, traversal, symlinks/reparse points and modified
artifacts are rejected. Older artifacts without ownership sidecars must be
copied into the workspace explicitly. Each input is at most 32 MiB. This scope is an
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
gets one worker dispatch; select-all on a focused native Edit/RichEdit control
uses one scoped Win32 message instead of unreliable Ctrl+A simulation.
Windows literal input targets the confirmed focused Edit/RichEdit control and
preserves its selection; it does not move to the end or choose another editable
field. Password/read-only controls are denied.
Coordinates require unchanged window bounds. A crash
after input is an uncertain outcome requiring observation, not automatic replay.

macOS single-element clicks use one exact AXPress action, bypassing heuristic
descendant/hit-test attempts after an uncertain result. Literal Unicode input
uses one PID-targeted event path with focused-window/element checks between
chunks; it does not append via AX and then repeat via keyboard on uncertainty.
Text select-all uses the focused element's AX selection range.

Screenshots are returned once as image content. Native base64 data URLs are
removed from JSON text. The displayed image is bounded to 1280 pixels per edge
and 1M pixels total; coordinates are mapped back to the original native
screenshot before dispatch. Accessibility text is bounded with explicit
truncation. Action replies request a fresh observation instead of duplicating
a second full image/tree.

Click/set-value prefer observed accessibility elements. Coordinate clicks use
app-targeted input. Keyboard text requires the selected window's keyboard focus;
Windows additionally requires it already be foreground. The bridge does not
activate it. Navigation and select-all/address-bar shortcuts are supported.
Text select-all requires an editable text element (native Edit/RichEdit on Windows).
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

An initial native GPT-Live request returned 400 because it used the wrong
session schema. The source-matched Codex 0.166 V3 request subsequently returned
201 with an SDP answer. This is signaling success only: the tested ICE-TCP
tunnels connected but received no return bytes; ICE failed, no data channel or
audio arrived, and the matching sideband returned 404. A public primary
WebSocket upgrade also failed to initialize a usable session. No microphone,
speaker, camera or user content was used. Full-duplex terminal voice,
video/screen streaming and mid-turn steering remain unverified.
An independent isolated Chromium check used a synthetic silent track, with no
microphone, playback, everyday profile or browser-held credential. The native
answer accepted both audio and data sections and advertised UDP/TCP candidates,
but the peer failed before a working data channel or audio. Its final ICE stats
do not identify the network or account cause. This is retained as another failed
transport check, not a working browser fallback.
The [upstream sideband report](https://github.com/openai/codex/issues/35094)
describes a similar 201/404 sequence; it does not prove the cause of this
installation's separate ICE failure. Other native voice
[service errors](https://github.com/openai/codex/issues/55531) also require
their own evidence rather than a generic transport diagnosis.
[Claude channels require Anthropic authentication](https://code.claude.com/docs/en/channels).
An audio MCP exchange does not remove that host gate or create a full-duplex
terminal UI. No PTY keystroke injection or client binary patch is used.

Legacy `/v1/responses/compact` returned 404. The later
[native V2 implementation](NATIVE_COMPACTION.md) uses the ordinary Responses
stream, passed real Claude compact/resume/fork checks, and is available with
`--native-compaction` on the reviewed subscription runtime. Claude still owns
the boundary. Default launches use ordinary summaries, and normal task effort
is preserved. Large real-task latency and old client startup windows remain
tracked in [OPERATIONS.md](OPERATIONS.md).

## Repeatable checks

Install the hash lock in a disposable environment, then run:

```text
python -m unittest discover -s tests -v
python tests/probe_native_mcp_client.py
python tests/live_desktop.py --keyboard
```

The first two use synthetic providers; the second uses real Claude without a
model account. Desktop compiles and owns an ordinary AppKit/WinForms fixture,
verifies OS state changes and uses no test command bridge. It never captures
another user window. macOS needs existing OS permission. CI runs account-free
tests on all three OSes and the GUI fixture on native Windows. Live subscription
media checks consume quota and keep raw files private.

## Local end-to-end evidence

`tests/live_native_mcp.py` ran through the installed launcher and frozen MCP
server using the existing subscription: real Claude ToolSearch, PDF extraction,
one image generation, editing that returned artifact, and Realtime voice all
passed in 129.250 seconds. Requested image model: `gpt-image-2.5`. Requested and
delivered sizes were 1024×1024; provider originals were 1536×1024 and 1254×1254.
The voice session reported `gpt-realtime` and returned a 2.45-second WAV. Existing
origin/ingress state was unchanged. This tests the workflow, not semantic image
quality or billing equivalence.

`tests/live_desktop_claude.py` used the current Terra alias in real Claude: three
observations and two actions changed an ordinary AppKit field and invoked its
button exactly once in 33.704 seconds. Earlier trials failed: ambiguous numeric
element types were rejected, and the heuristic click path produced extra
button invocations despite a requested count of one. Those failures motivated
the explicit schema, numeric-index normalization and exact AXPress path; they
are not counted as passes. A separate native fixture passed coordinate click,
text select-all and Unicode/emoji input on macOS without global pointer input.

The [three-OS run at 788371c](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41094133443)
passed all jobs, including the owned native Windows screenshot/text/button
fixture. The subsequent keyboard extension exposed ineffective Windows Ctrl+A;
the focused text-control selection/insertion fixes then passed the full
[Windows Unicode GUI gate at ecf5b1b](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41099866806). No model
credential is sent to CI. Installed frozen MCP startup was also checked with a
malicious synthetic workspace `sitecustomize.py`; isolated Python ignored it
and read the real authenticated image catalog successfully.

## Dependency security gate

The initial Pillow 12.2 pin was superseded after OSV and
[Pillow's own advisory](https://github.com/python-pillow/Pillow/security/advisories/GHSA-22hg-r758-rrw9)
identified affected versions through 12.3. The lock now requires Pillow 13.0.0,
with new wheel hashes and regression checks. Image inputs open only the allowed
PNG/JPEG/WebP decoders; desktop screenshots open only PNG. The post-update OSV
query completed for all 33 public dependency pins with no matching advisories.
This is a dated database result, not proof of absence of unknown vulnerabilities
or of binary provenance. `python tests/audit_capability_lock.py` repeats the
check and fails on findings/incomplete responses; CI runs it without accounts.
Only already-public package names/versions are sent to OSV.
