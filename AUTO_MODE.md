# Auto mode on Codex-backed routes

HarnessLift defaults `CLAUDE_CODE_AUTO_MODE_SERVER=0` for new subscription and
Project API launches. Claude Code keeps its own action classifier and permission
rules. An explicit operator value is preserved. No permission mode is changed.

The [official classifier-billing guide](https://code.claude.com/docs/en/auto-mode-classifier-billing)
requires Anthropic's `safeguards` request field and matching beta header, plus
real `safeguard_results` in responses, for server review. This Codex/Responses
adapter has no verified equivalent. It does not fabricate results or free-service
eligibility. Claude documents client review as the supported configuration for
such gateways. The setting is temporary upstream and must be rechecked when
updating the locked client.

If an existing session is waiting at the classifier-charge notice, Enter keeps
the session in auto mode with client review. Esc or Ctrl-C cancels the held
action. The launcher default applies to new processes; existing sessions are
not restarted or sent keyboard input.

Classifier inference uses the configured route. Provider-specific quota and
billing still apply; this change supplies no Anthropic entitlement or credits.
The [classifier-model setting](https://code.claude.com/docs/en/permission-modes#choose-the-classifier-model)
`CLAUDE_CODE_AUTO_MODE_MODEL` is preserved. Use a full available model ID from
`harnesslift models` when choosing it separately; no faster reviewer is selected
automatically.

The [native-client fixture](tests/probe_auto_classifier.py) uses fake models,
disposable homes and owned file effects. Local direct Messages and two-hop
Codex controls passed both launch profiles: allow runs once, deny runs nothing
and retains its reason, and an explicit deny rule still blocks an allow verdict.
The [three-platform gate](.github/workflows/auto-classifier-compatibility.yml)
is pending. These headless checks do not establish real-model review accuracy,
Windows interactive behavior, server-side review or zero classifier usage.
