# Nested agents and task-stop ownership

An ownership-shaped `TaskStop` denial appeared in local child-session metadata.
The retained records do not establish which agent created the target or whether
it is currently running. The message shape resembles a report in the official
[Claude Code issue tracker](https://github.com/anthropics/claude-code/issues/75043),
but a similar message alone does not establish the same root cause.

## Isolated observation

`tests/probe_nested_task_stop.py` runs the integrity-locked Claude Code 2.1.316
client against a fake local Messages service, in a disposable home and workspace.
It uses no model account or real project. The child agent is held at the fake
service until the stop result is observed; its only possible side effect is a
synthetic file inside the disposable workspace. The environment is filtered,
but this is not an operating-system sandbox.

On this Mac, a main session spawned an intermediate agent, which spawned its own
background child. While that child was held open, the intermediate agent's
`TaskStop` returned an ownership-shaped error. Afterward, the child made the
synthetic file write and returned a tool result. In the independent root-session
control, `TaskStop` reported success and no corresponding file appeared during
the bounded observation. This is a direct-client, fake-model observation, so
it shows a harness behavior without implicating the HTTP bridge. It does not
prove every stop or descendant effect behaves the same way, nor does it identify
the cause of a particular real-session denial.

The optional `--observe-hooks` variant also tests `PostToolUseFailure` and
`PostToolBatch` hooks with synthetic input. On this Mac both detected the
synthetic ownership error and their fixed additional-context markers reached
the fake model endpoint. The successful root stop did not produce a matching
failure marker. The [hooks reference](https://code.claude.com/docs/en/hooks#posttoolusefailure)
describes event boundaries and warns that error text is not a stable format.
This demonstrates an observation path, not an automatic remedy or proof that a
real model will act on additional context. No hook has been installed into user
settings, and no task or permission boundary has been bypassed.

To reproduce locally after installing the locked fixture:

```sh
CLAUDE_CODEX_TEST_BIN="$(python tests/claude_fixture.py)" \
  python tests/probe_nested_task_stop.py --expect-ownership-denial \
  --observe-hooks --expect-hook-observation
```

The explicit expectations are specific to this pinned client and observation;
omit them to report changed behavior in a future client. Verify a mitigation
with independent task effects, actual notification routing, permission-denial
controls, resume and interruption before enabling it. A failed `TaskStop` must
not be treated as successful cancellation. Do not terminate another session's
tasks by process name or infer that a task is stopped from an old transcript.
