# Changelog

HarnessLift versions describe this wrapper and documentation, independently of the pinned CLIProxyAPI or Claude Code version.

## Unreleased

- Include observer-only stream endings in the read-only cross-layer timing
  report, even when no separate error log was written. Label the evidence
  source, omit explicit observer cancellations and preserve ambiguous matches.
- Extend credential-free POSIX cancellation acceptance to interrupted file
  tools in the same interactive client. Check retained successful results,
  one effect per target and independent work at two byte-identical client
  paths. Existing user homes and provider-hosted actions remain separate gates.
- Add a source-locked Claude Messages error compatibility patch: preserve the
  status-derived native category for foreign `server_error` and
  `service_unavailable_error` names. This enables the tested Claude client's
  native recovery after opaque-signature stream failures without adding
  gateway replay. Include credential-free baseline/candidate, tool-safety,
  interactive and delayed-reasoning gates. See [STREAMING.md](STREAMING.md#native-claude-error-compatibility-2026-11-07)
  for the verified boundary and remaining limitations.

## v0.1.0-preview.1 — 2026-11-06

First public preview under the HarnessLift name. The project was previously `claude-codex-http-bridge`; Git history and existing compatibility documentation are retained.

- Add the `harnesslift` entry point on POSIX and Windows alongside the compatible `claude-codex` command. Both use the same private installation, account selection, and runtime settings.
- Check both command names for conflicts before installation writes. Preserve exact managed legacy wrappers and the existing Windows wrapper upgrade path.
- Show the selected entry point in newly installed POSIX tmux wrappers, preserving exit status and automatic-rename cleanup.
- Introduce an English project overview, preserve the detailed guide in `USAGE.md`, and add original visual assets, a synthetic client walkthrough, contribution guidance, and privacy-conscious issue templates.
- Update canonical GitHub links and check both installed command names in Linux/Windows installation CI.

This release does not update an existing installation automatically, migrate credentials, restart services, or resolve upstream stream interruptions. Controller source installation is still not an atomic live update. Stream-recovery prototypes remain experimental and are not distributed as a production runtime. See [STREAMING.md](STREAMING.md), [RUNTIME.md](RUNTIME.md), and [PLAN.md](PLAN.md).

The installer continues to download the pinned upstream CLIProxyAPI v8.0.46 archive, verify its SHA-256, and retain its license. Reviewed runtime extensions require separate setup. Optional native tools are also installed separately. No model credentials, provider binaries, user transcripts, or runtime logs are included in this release.
