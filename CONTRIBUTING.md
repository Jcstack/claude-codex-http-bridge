# Contributing to HarnessLift

Help us make Claude Code work reliably with Codex-backed models while keeping the local harness and its permission boundaries intact. Evidence and reproducible cases matter more than a claim of universal compatibility.

## Before opening an issue

Read the [compatibility matrix](COMPATIBILITY.md), [streaming investigation](STREAMING.md), and [current plan](PLAN.md). Identify whether the behavior comes from Claude Code, the bridge controller, the pinned translator, an optional runtime extension, or the upstream model service. It is fine to say the cause is unknown.

Use the issue templates to describe the smallest synthetic reproduction. Include the OS, versions, route type, whether a reviewed runtime is selected, and a short redacted error category. Do not attach account identifiers, email addresses, access/refresh tokens, config files, user prompts, private repository paths, raw transcripts, or full logs. Keep any diagnostic output local until you have reviewed and redacted it. For a security issue, follow [SECURITY.md](SECURITY.md).

## Making a change

1. Use a dedicated checkout and a disposable installation root. Do not reinstall source, roll a runtime, or stop a service used by active tasks as part of development.
2. Keep changes focused. Preserve the compatible `claude-codex` entry point and existing private directory, environment, account, and service identifiers.
3. Add a meaningful regression case when behavior changes. Prefer fake loopback endpoints, synthetic credentials, and isolated homes/workspaces. The ordinary test suite must not require a paid model account.
4. Update the relevant English documentation. State what was observed, which route/client/runtime was exercised, and what remains unverified. Distinguish protocol fixtures from real model performance or entitlement.

Run the offline Python suite from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

Some optional-capability cases require the hash-locked environment described in CI. Gateway changes also need the Go checks:

```bash
cd gateway
go test -race -timeout 60s ./...
```

The workflow in [`.github/workflows/ci.yml`](.github/workflows/ci.yml) contains installation checks and real-client fixtures. The Claude fixture uses its integrity-locked npm package with install scripts disabled; model responses are synthetic. Follow the workflow for platform-specific setup rather than calling a real provider during ordinary tests.

## Before committing

The repository uses an explicit source allowlist. Add only the necessary source paths to `.gitignore`; do not broaden it to include runtime directories. Inspect the staged diff and scan it:

```bash
git diff --cached --stat
git diff --cached
gitleaks git --staged --redact --no-banner --no-color
```

For public releases, scan the complete Git history as well. Render and inspect changed visual assets; keep demo inputs synthetic and explain how the assets were produced. Never turn an unresolved reliability issue into an advertised guarantee.
