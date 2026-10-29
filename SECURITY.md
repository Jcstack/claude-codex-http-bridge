# Security

Never commit or publish OAuth JSON, access or refresh tokens, API keys, `config.yaml`, `proxy.sha256`, proxy binaries, logs, Claude transcripts, model caches, or files from an installed runtime directory. The private GitHub visibility is an additional boundary, not permission to store credentials in Git history.

The proxy writes OAuth credentials only after the user completes `claude-codex codex-login`. On POSIX the installer creates the installed root and OAuth directory with mode `0700`, and config/credential files with mode `0600`. On native Windows it requires NTFS and applies protected ACLs that allow only the current user to access the installation and session data. Each launched Claude Code session receives a fresh localhost key. The proxy accepts requests only on `127.0.0.1` and stops with the foreground session.

Review updates to the pinned proxy release and its security advisories before changing the version or hashes. Do not enable LAN binding, request body logging, or the management interface without a separate security review.

If a credential is accidentally committed, revoke or rotate it first, then remove it from the repository and its full Git history before any further push. Deleting only the latest file does not remove it from earlier commits.
