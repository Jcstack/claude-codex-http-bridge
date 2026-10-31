# Independent Codex accounts

Claude Code remains the harness. Account selection changes the credential used
by its existing local HTTP gateway. It does not start a Codex agent, replace
Claude's conversation, or change the native Codex CLI/app login.

## Switch an already-running bridge

These commands work in macOS/Linux terminals and native Windows PowerShell:

```text
claude-codex account login work
claude-codex account check work
claude-codex account use work
claude-codex account list
```

Choose the other ChatGPT account in the browser. `login` only stages its
credentials privately; current requests keep their original account. `check`
uses a disposable private validator for staged/inactive credentials and a small
synthetic inference request. Checking the already-selected account reuses its
existing route so there is no second refresh owner for those credentials.
An installed inactive account also retains its original refresh owner:
CLIProxyAPI continues refreshing disabled credentials. Its validator receives
only an access-token snapshot, with outer and nested refresh grants excluded,
and cannot write changed credentials back. Only a new, uninstalled staged grant
can refresh inside the validator. An expired/revoked access snapshot can fail
validation; it is not repaired by racing another refresh owner.
`use` repeats that check if no recent successful check exists. It enables and
publishes the validated target before disabling other Codex credentials.
It never deletes credentials, stops a proxy, or restarts a Claude process.

The selection applies to all sessions sharing this bridge, including older
sessions still connected directly to its original localhost port. Already
admitted generation streams finish using their old account; subsequent
requests use the selected account. An already-open native Realtime/WebSocket
connection retains its upstream account until that connection ends.

To return to an old account, run `account list` and pass its retained alias to
`account use`. Aliases generated for older installations contain a truncated
SHA-256 identity, never an email address. A failed switch restores the previous
enabled/disabled selection. If that restoration is interrupted or fails, the
private transaction journal remains; run:

```text
claude-codex account recover
```

Earlier validators could rotate an inactive record's refresh token. Recovery
support for those private refresh checkpoints remains: an alias with a pending
checkpoint is blocked until `account recover` succeeds. Recovery preserves
enabled/disabled selection and refuses to overwrite a newer generation. New
installed-account checks avoid this competing refresh path entirely. Checks,
switches and recovery share the same local lock.

## Security and quota

Authorization uses PKCE, a random callback state, a loopback-only
`127.0.0.1:1455` listener, and fixed OpenAI authorization/token endpoints. An
occupied callback port is not killed. Tokens and callback URLs are not printed.
New credentials stay under the private installation directory, outside the
source repository. On Unix, directories use 0700 and credential files use 0600;
Windows uses the installation's restricted NTFS ACLs. The source allowlist does
not admit runtime account data. `codex-login` uses this same login flow for the
first account; with existing credentials it stages a new alias and preserves
the current selection.

Account identities include the user as well as the workspace, so different
people in the same organization cannot share an alias or a quota cache.
Quota caching follows exactly one enabled bridge account. Disabled backups do
not make it ambiguous. A cached result from a different account is rejected.
When native Codex uses the same account, its `account/rateLimits/read` RPC is
available. Otherwise the helper reads the selected account's fixed ChatGPT
usage endpoint directly over verified HTTPS, without ambient proxies or
redirects. It does not overwrite native Codex auth to make the accounts match.
Only actual five-hour/weekly windows are shown; missing windows are not
invented. The cache stores reported percentages, reset times and an account
fingerprint, never a token or the raw usage response.

Prompt caches may need to warm again. Model access, available quota and
service latency depend on the selected account and upstream. Existing Claude
startup model/context settings are unchanged; account switching cannot add a
model entitlement or change those startup settings. Long-lived native
connections, every possible encrypted native item, and every future upstream
protocol are not certified by the handoff tests.

## Verification

The credential-free account tests cover isolated OAuth staging, callback state,
private files, alias traversal, rollback ordering, disabled backups, stale quota
caches, and fixed-host usage requests. The pinned upstream test
`tests/auth_handoff_fixture.go` verifies that disabling an account does not call
its execution-session closer, mutate an admitted request's credential snapshot,
cancel its context, or re-enable it when an old stream finishes.

A local account handoff also completed while a synthetic generation stream was
still running. That stream spanned the switch and finished cleanly after about
70 seconds. Origin PID/port, managed TLS state and native Codex auth remained
unchanged. New-account inference, tool-result continuation and replay of two
native hosted-search state items passed. Separate encrypted reasoning blocks
were not observed in those trials; their cross-account replay is unverified.
These live checks used synthetic content. Account files, response bodies and
private logs are not published, and CI receives no real account credentials.

A later live check of the retained inactive account completed inference in
3.354 seconds with no outer or nested refresh grant supplied to its validator.
The currently selected account and origin service stayed unchanged. The
access-only rule follows the pinned upstream's
[disabled-credential refresh tests](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/sdk/cliproxy/auth/conductor_refresh_disabled_test.go),
not an assumption that disabled credentials cannot refresh.
