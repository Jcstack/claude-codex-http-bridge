# Reviewed runtime rollout

The launcher can keep a fixed, authenticated TLS endpoint for new Claude Code
sessions while replacing translator processes behind it. Existing sessions on
the original direct service keep their listener and tasks. The original service
also supplies the authenticated Codex model route; neither OAuth nor an OpenAI
project key is copied into the new translator.

```text
Claude Code -> stable TLS ingress -> reviewed translator
                         ^                  |
                         |-- TLS origin guard
                                  |
                          existing Codex HTTP service -> OpenAI
```

The extra localhost hops are a migration arrangement. They do not start another
agent or change which application owns permissions, tools and conversation.
Only `/v1/messages` and `/v1/messages/count_tokens` use the compatibility
translator. Native Responses, model discovery, image/media and other `/v1/`
routes pass through the guarded origin with their bytes intact. This preserves
the origin's API surface; it does not establish model/account entitlement to
every hosted tool. Native requests participate in the active-request stop guard.
Retiring the original service requires its dependent sessions to finish. Moving
an already-running client from its old endpoint is not implemented by editing
its environment or settings files.

## Build and install

Use a trusted build from the candidate workflow, or reproduce its pinned source,
reviewed patches, `go mod verify`, unit/contract tests and Go build commands in
[candidate-proxy.yml](.github/workflows/candidate-proxy.yml). Its runtime bundle
contains two executables and a manifest, with platform, upstream revision,
patch/source hashes and binary hashes. It contains no runtime data. Hash checks
establish consistency with that manifest, not an independent publisher signature;
build/download provenance still matters.

To pack binaries from the reviewed build:

```text
python runtime_bundle.py --proxy /path/to/cli-proxy-api --ingress /path/to/bridge-ingress --output /private/empty-bundle-directory
claude-codex bridge upgrade /private/empty-bundle-directory
claude-codex bridge status
claude-codex --model sol
```

Install matching launcher source before activating a new source lock. On
Windows the bundle uses native `.exe` files; the packer accepts the actual build
paths. A mismatched platform, patch/source lock, binary checksum or path is
rejected before changing the chosen runtime. Installed bundles are immutable
directories identified by the manifest hash. They never overwrite a running
executable, which also supports Windows file locking.

A previously selected bundle remains usable when launcher source is updated:
its private selection must still match the entire immutable manifest and both
binary hashes. This does not admit an old or unknown bundle as a new upgrade.
New activation always requires the current reviewed source lock. Changing a
selected manifest, even to the new source hash, fails its recorded identity.

Once installed, ordinary new `claude-codex` launches use the managed endpoint.
The model catalog and HUD still come from the authenticated subscription route.
No OpenAI API project route is enabled. `claude-codex bridge reload` starts and
checks a replacement, atomically changes new-request routing, and stops retired
backends only after their admitted request count reaches zero. A request is never
replayed after partial output. Replacement failure restores the previous
manifest and state. Pending drain work can be completed by another reload.

`bridge stop` refuses while managed requests are active. As with the original
service, wait for background tasks before stopping; a task can be between model
requests even when the instantaneous request count is zero.

## Native URL citations

The [URL annotation patch](upstream/patches/url-citations.patch) maps OpenAI
`url_citation` annotations into Claude text citations, including incremental
`citations_delta` events. It preserves the answer text, URL, title and native
offsets. Repeated annotation/part/item/terminal events are deduplicated; a
non-streaming response and a real Claude resume are also covered by the fixture.

`encrypted_index` carries a namespaced bridge continuation envelope, not an
Anthropic encrypted index. It includes the native annotation and an already
emitted text-prefix hash. The request translator restores annotations only in
assistant text with matching prefix, URL and title; edited or merged text does
not receive stale offsets. This hash checks consistency, not authenticity.
Envelopes are bounded to 64 KiB per annotation and 1,024 citations per response.

OpenAI's [URL annotation](https://developers.openai.com/api/docs/guides/tools-web-search#output-and-citations)
locates a citation in generated output; it does not supply a quoted source
excerpt. The [Claude citation format](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool#citations)
also has `cited_text`, which this bridge leaves empty. Link display and replay
must not be described as verified quotation accuracy. File/container citations,
commentary annotations and complete native citation UI equivalence remain open.

## Local identity and credentials

The ingress binds only `127.0.0.1`. It generates a private local CA and a server
certificate limited to loopback. Claude trusts this CA through the documented
[`NODE_EXTRA_CA_CERTS` setting](https://code.claude.com/docs/en/network-config#custom-ca-certificates),
preserving any existing extra CA file. The CA is not added to the operating
system trust store. A fixed TLS identity prevents a different local listener
from receiving a client's bearer key after reclaiming the port. Leaf renewal
keeps the CA and running endpoint; the server retains the last valid key pair
while two certificate files are being replaced.

The translator trusts this CA only for one exact HTTPS loopback authority and
does not follow its redirects. Public-provider TLS roots and certificate
verification remain enabled. The origin guard uses a separate local key and
checks the legacy listener PID after connecting, before sending the legacy key.
Frontend credentials cannot use the guard route, and neither route exposes the
upstream management API.

Runtime keys, certificates, configs and logs stay under the private installation
root, with POSIX private modes or current-user NTFS ACLs. Per-launch settings
carry the gateway and CA configuration so detached sessions can load them;
the user's global settings are not rewritten. The separate compatibility
translator has no OAuth files. Its retries are disabled because the existing
Codex service handles upstream retries. Its transient-error cooldown is disabled
so a recovered localhost fault does not create another minute of 503 responses;
quota/429 behavior is retained.

Proxy processes start in their own private runtime directories so they do not
automatically load a user's project `.env`. Claude Code still starts in the
requested project. Background fixtures use a separate `CLAUDE_CONFIG_DIR`
[supervisor instance](https://code.claude.com/docs/en/agent-view#where-state-is-stored).
The warm-supervisor test records a real background model reply after starting
that supervisor with a deliberately wrong gateway, then dispatching with the
private per-session profile. It tests routing/reply delivery, not the mock
model's task-state classification.

## Acceptance scope

The credential-free full-path probe verifies stable TLS routing, old-stream
drain, startup rollback, active-stop refusal, PID rejection before credentials,
immediate recovery after a restored origin, and two turns in the same real
Claude process separated by a backend reload. It uses a disposable fake model
and configuration. Live subscription checks and native Windows execution are
reported separately in [OPERATIONS.md](OPERATIONS.md).

This arrangement does not by itself repair upstream TLS failures, make every
cache lookup hit, change an existing client's startup compaction setting, or
certify every Claude plugin. Those require their own measured checks.
