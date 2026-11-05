# Coding quality: model, harness and translation

Checked 2026-10-30. This record asks whether an observed difference between GPT/Codex and Claude in coding comes from the model, the agent harness, or this Anthropic-to-Responses bridge. Those are separate variables. No public benchmark below evaluates **this exact Claude Code → CLIProxyAPI → GPT-6.1 Sol** configuration on a representative coding suite.

Rechecked the three headline comparisons on 2026-11-02: the published
FrontierCode 2.0 leaders, Coding Agent Index comparison, and Composio Astra
matrix remain as stated below. FrontierCode's score includes its weighted
rubric after blocking checks; it is not simply a binary task pass rate.

Rechecked 2026-11-05: the local subscription-proxy catalog also lists
`gpt-6.1-terra`. The catalog is not a verified entitlement list for every
selected account. In Artificial Analysis' common Stirrup harness,
[Terra Max and Astra Max both round to 42% on FrontierCode 2.0](https://artificialanalysis.ai/models/comparisons/gpt-6-1-terra-vs-gpt-6-astra);
[Sol Max rounds to 41%](https://artificialanalysis.ai/models/comparisons/gpt-6-1-sol-vs-gpt-6-astra).
This is a weighted benchmark score, not a binary pass rate or proof of parity
inside Claude Code. On the same comparison pages Astra scores higher than Terra
on Terminal-Bench 4.0 and the broader Intelligence Index. Treat Terra as a
coding candidate, not as an established replacement for Astra on every task.

[Endor Labs' 179 scored secure-coding tasks](https://www.endorlabs.com/learn/gpt-6-1-sol-on-codex-astra-level-security-a-third-faster-zero-cheating)
reported a 7.9-minute median for GPT-6.1 Sol and 11.6 minutes for Astra in
Codex CLI on Azure, with functional pass rates of 77.7% and 82.1%, respectively.
The comparison does not use this subscription route or Claude Code. On
Artificial Analysis' broader Intelligence Index, Astra's reported average time
per task is lower than Sol's despite Sol's higher output-token speed. These are
different task sets and measures; neither predicts an individual session's
latency. [OpenAI's model guide](https://developers.openai.com/api/docs/guides/latest-model)
positions Astra for maximum capability and Sol 6.1 for a capability/speed
tradeoff. Do not apply API prices, throughput or limits to a Codex subscription.

| Comparison | What it isolates | Current evidence and limit |
| --- | --- | --- |
| Different models, one harness | Mostly model and inference settings | [Artificial Analysis FrontierCode 2.0](https://artificialanalysis.ai/evaluations/frontiercode-2-0) runs 132 private coding tasks three times per model in the same Stirrup harness. Its current Max scores are Opus 5.5 **46.4%**, GPT-6 Astra **42.4%**, GPT-6.1 Sol **40.6%**, and Fable 5.1 with fallback **31.6%** ([comparison table](https://artificialanalysis.ai/models/releases/comparisons/gpt-6-1-sol-vs-claude-fable-5-1)). This supports a model-level gap between Opus and the tested GPT models on that suite; it does not prove a universal ranking or evaluate this bridge. The Claude fallback policy is part of the reported model configuration. |
| Same model, different harness | Harness plus its tools, prompts and serving route | A [private paired study](https://arxiv.org/abs/2609.11987) ran 80 matched tasks per contrast: GPT-5.5 in Codex SDK vs DeepAgents differed by **+1.25 percentage points**, task-bootstrap 95% CI **[-4.4, +6.9]**; Opus 4.8 in Claude SDK vs DeepAgents differed by **-1.25 points**, CI **[-10.0, +7.5]**. Neither contrast establishes a mean winner, and neither directly compares Claude Code with Codex CLI on GPT-6.1 Sol. The study also found task-family variation and corrected a cache-token accounting error. |
| Small direct GPT harness comparisons | Task-specific execution and efficiency | [Composio Bench](https://composio.dev/bench/compare/harnesses) shows GPT-6 Astra at **21/29** in Claude Code and **22/29** in Codex on its displayed task set. These are not a large representative software-engineering suite, and a one-task difference is weak evidence. A [community harness benchmark with public raw cases](https://github.com/d1-m4ss/harness-benchmark) found the same Muse model and bug-fix task passed 2/2 in both Claude Code and Codex, with similar wall time and cumulative input; its separate GPT-5.6 Luna series does **not** include a verified Claude Code cell, so it cannot settle the GPT harness comparison. |
| Vendor harness, different model | Real product combination | [Artificial Analysis' Coding Agent Index v2.0 comparison](https://artificialanalysis.ai/agents/coding-agents/comparisons/claude-code-vs-codex) reports **62** for Opus 5.5 Max in Claude Code and **55** for GPT-6 Astra Max in Codex, while the latter uses fewer tokens and less active time on that suite. Its [methodology](https://artificialanalysis.ai/methodology/coding-agents-benchmarking) combines repository changes, terminal work and repository Q&A. Both the **model and harness change**, so the seven-point gap cannot be assigned to either alone. [Cognition's FrontierCode leaderboard](https://cognition.com/frontiercode) likewise evaluates model-and-harness pairings and explicitly says its plotted points are shaped by harness. |

**Current inference:** both model and harness matter, while the bridge introduces a third measurable factor. The independent same-harness data show that the GPT models are competitive with Fable in coding and trail Opus 5.5 on this specific FrontierCode setup. The paired harness study does not support claiming that Claude Code automatically upgrades a GPT model to Opus quality. The original pinned proxy lost `tool_addition` and deferred-tool semantics; the reviewed managed runtime corrects those specific behaviors in isolated protocol and client tests. That removes known defects but does not establish representative coding quality or compatibility with every future tool feature. These claims are narrower than “GPT is weaker” or “Claude Code is always the better harness.”

## Can GPT improve inside Claude Code?

There is **some task-specific evidence for yes**, but no representative result that GPT + Claude Code beats GPT + Codex on real repository changes, or equals Opus 5.5 + Claude Code overall:

- [Composio's current same-model matrix](https://composio.dev/bench/compare/harnesses) gives GPT-6 Astra **21/29** with Claude Code and **22/29** with Codex. Its [research article](https://composio.dev/bench/research/agent-harness-benchmark) reports an earlier-looking **20/29 vs 21/29** snapshot; both put Claude Code one task behind, and the one-task discrepancy is a reason not to treat this small set as a stable universal rank. The article reports Claude Code used more tokens and time in this GPT-6 Astra MCP workflow. These are integration tasks, not a representative merge-quality coding suite.
- The new [TraceTrial-Bench 95-task matrix](https://tracelite.ai/data/leaderboard.json) reports **GPT-5.6 Sol in Claude Code at 44.2% task pass versus 41.1% in Codex** on the same mechanism-focused tasks. Its [protocol](https://tracelite.ai/trial-by-trace.html) retains one selected run per model × harness × task cell, with no repeated-run uncertainty estimate. The tasks emphasize context, tool delivery, permissions, session state and observability rather than maintainer-reviewed repository patches. This is real evidence that Claude Code can help one GPT model in some scenarios; it is not evidence for GPT-6.1 Sol or for Opus 5.5 parity.
- [The paired private-suite study](https://arxiv.org/abs/2609.11987) still finds no resolved average harness winner on its different GPT-5.5/Claude-SDK-versus-DeepAgents contrasts. Harness gains are model- and task-dependent.

## Other candidates for the Opus 5.5 + Claude Code target

The [independent FrontierCode 2.0 setup](https://artificialanalysis.ai/evaluations/frontiercode-2-0) holds its Stirrup harness and 132 repository tasks fixed, with three runs per task. Its scores are **model evidence**, not a guarantee about a Claude Code route. Terminal-Bench 4.0 instead uses [66 terminal tasks, mini-swe-agent and three repeats](https://artificialanalysis.ai/evaluations/terminalbench-4-0). The two task families can rank a candidate differently.

| Model | Real-repository FrontierCode 2.0 | Terminal-Bench 4.0 | Practical interpretation |
| --- | ---: | ---: | --- |
| Claude Opus 5.5 Max | 46.4% | about 59.6% | Target; native Claude Code route avoids an extra translator. [Source](https://artificialanalysis.ai/evaluations/frontiercode-2-0) |
| GPT-6 Astra Max | 42.4% | about 59% | Leading GPT on broad reasoning and a strong repository result in this set; this does not measure GPT through our bridge. [Source](https://artificialanalysis.ai/models/comparisons/gemini-4-carbon-vs-gpt-6-astra) |
| GPT-6.1 Sol Max | about 41% | about 56% | Strong lower-cost GPT route; a Claude Code quality lift is unproven. [Source](https://artificialanalysis.ai/models/comparisons/gpt-6-1-sol-vs-gpt-6-astra) |
| GPT-6.1 Terra Max | about 42% | about 56% | Near Astra on this repository suite, with a lower broad Intelligence Index score; test task-specific quality and account access. [Source](https://artificialanalysis.ai/models/comparisons/gpt-6-1-terra-vs-gpt-6-astra) |
| Gemini 4 Carbon High | about 41% | about 65% | Close on these two tasks, but [Google currently restricts access to selected partners](https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-gemini-4-carbon/); not an installable general-user option. [Scores](https://artificialanalysis.ai/models/comparisons/gemini-4-carbon-vs-claude-opus-5-5) |
| Gemini 4 Argon High | about 26% | about 57% | [Public API exists](https://ai.google.dev/gemini-api/docs/latest-model), but current repository benchmark does not support Opus-level coding. [Scores](https://artificialanalysis.ai/models/comparisons/gemini-4-argon-vs-claude-opus-5-5-xhigh) |
| Kimi K3.1 Pro | about 25% | **67.2%** | Strong terminal specialist, weaker on merge-quality repository changes. [Independent scores](https://artificialanalysis.ai/models/comparisons/kimi-k3-1-pro-vs-claude-opus-5-5-xhigh); [Kimi officially supports direct Anthropic Messages in Claude Code](https://www.kimi.com/code/docs/en/third-party-tools/claude-code.html), with plan-dependent 256k/1M context and mandatory thinking. |
| DeepSeek V4.1 Flash 1017 Max | about 28% | about 34% | Attractive for cost and narrower workflows, not an Opus replacement on these suites. [Scores](https://artificialanalysis.ai/models/comparisons/deepseek-v4-1-flash-1017-vs-claude-opus-5-5) |

Inside the [Coding Agent Index v2.0's actual Claude Code variants](https://artificialanalysis.ai/agents/coding-agents/comparisons/claude-code-vs-codex), Opus 5.5 scores **62**, Sonnet 5.5 **59**, Fable 5.1 **58**, Step 5 Preview **43**, and Qwen3.8 Max **39**. This supports the Anthropic family as the currently measured near-Opus choices in that particular Claude Code suite. It does not include GPT + Claude Code or the new Kimi K3.1 Pro route.

**Decision for this project:** keep GPT-6 Astra/Sol as the first same-model harness experiment and Kimi K3.1 Pro as a separate, direct-Messages terminal specialist to evaluate only with an explicitly supplied account. Do not infer parity from a proxy's feature list, a vendor benchmark, a single task, or a model's peak score on a different task family. The exact Opus 5.5 + Claude Code target still needs a paired local evaluation on the user's own safe, isolated repository tasks and independent patch review.

Kimi's [official Claude Code setup guide](https://www.kimi.com/code/docs/en/third-party-tools/claude-code.html) includes a script that edits global Claude settings and an example that stores the API key in plaintext `settings.json`. A privacy-preserving pilot should instead use a dedicated session profile and private key source, keep normal `claude` sessions untouched, verify the actual served model, and select the account's real 256k or 1M limit. No Kimi credential has been configured in this project.

## Local evaluation needed before a quality claim

The [first local GPT harness pilot](HARNESS_PILOT.md) is now complete: both paths passed all seven external tests in both retained runs of one synthetic SSE repair task. Claude Code showed no quality gain and took longer in this sample. The report records the corrected personal-instruction control, unconfirmed native served-model identity, execution-order confound and token accounting. It is a successful functional pilot, not the representative matrix below. Opus was not run because no Claude account was available; the user also declined an API-key route.

Use the same model ID and effort for Codex CLI and `claude-codex`, with the same isolated repository revision, instructions, available local tools, permissions, time budget and acceptance tests. Repeat each task at least three times, include tasks that need search/MCP/subagents and longer context, and log only sanitized metrics: actual served model, final test results, independent patch review, tool-loop completion, cache tokens, wall time, and quota/spend. Include a direct Claude Code + Opus/Fable arm only when its account access and billing are explicitly available. The score should distinguish task success from latency and token use; a faster but incorrect patch is not a win.

The useful minimum matrix is **Opus 5.5 + Claude Code** (target), **GPT-6 Astra/Sol + Codex** (native harness control), **the same GPT model + Claude Code** (bridge effect), and **Kimi K3.1 Pro + Claude Code** (different model, same harness) when each route is legitimately available. Sample real repository bug fixes, multi-file refactors, terminal/environment work, search/MCP use, and long-context continuation separately. Run from clean disposable worktrees, hide reference fixes, use executable acceptance tests plus blind human patch review, and report each task family rather than only one blended score. Effort labels across providers do not represent equal compute, so compare both a matched latency/quota budget and each model's best practical setting.

Until that matrix is run, “150% capability” and Opus/Fable parity are goals, not measured results.

## Offline model-evaluation boundary, 2026-11-05

The [macOS boundary probe](tests/probe_model_eval_isolation.py) runs a copied
locked Claude client against a fake loopback Messages endpoint. A private
`CLAUDE_CODE_TMPDIR` keeps this fixture away from Claude's shared per-user temp
directory. Seatbelt denies user-home, mounted-volume and common temp reads,
outside writes and other network ports; Claude is limited to Read/Edit with
workspace-scoped permissions. Local synthetic checks passed for in-workspace
Read/Edit, rejected outside Read/Edit, a rejected outside symlink, and a blocked
second loopback port. A local unrestricted control also reached the owned
outside fixture paths and listener, confirming those checks can detect access
without the tested policy. No account, actual model inference, production
project, or running session was used.

This is not a complete adversarial sandbox: system-wide paths and non-network
IPC are not comprehensively constrained. It is a preparatory fixture, not an
approved live benchmark, and has no native Windows isolation equivalent yet.
Run the representative model comparison only after its privacy boundary,
independent acceptance and effect on active requests are verified.

The first [hosted macOS run](https://github.com/Jcstack/harnesslift/actions/runs/41867934739)
failed both its network and file preflight checks although the client completed
the synthetic tool sequence. The local Mac run passed. The initial hosted
output did not distinguish a preflight startup failure from a boundary
failure. A [diagnostic rerun](https://github.com/Jcstack/harnesslift/actions/runs/41870101094)
reported exit code 72 and developer-tool categories for both preflights, with
no observations. The system Python shim therefore failed to start inside the
policy. The fixture now compiles a minimal preflight before entering Seatbelt;
the [subsequent hosted run](https://github.com/Jcstack/harnesslift/actions/runs/41871305758)
passed the synthetic client, file and network checks. That hosted run preceded
the unrestricted negative control, which has passed locally only. These results
do not establish that the boundary is suitable for real model access.

A separate small subscription-route text check on 2026-11-05 requested
`gpt-6.1-terra` at `low` through the installed managed endpoint. The synthetic
stream completed in 2.969 seconds, returned the expected short answer, and the
translated response carried the requested model label. That label is not
independent served-model attestation. This single short request used no Claude
tools and does not establish coding quality, long-context behavior, general
latency, or access for a different account or future session. Existing sessions
and services were not restarted.

Another short synthetic request on the same date requested `gpt-6.1-sol`
with `xhigh` through the existing managed subscription endpoint. It completed
with the expected short text and requested model label in 2.999 seconds,
reporting 306 input and 17 output tokens. It used no Claude tools. The response
does not independently attest served identity or actual reasoning compute;
this small request is not comparable to a long-context coding task.

Separately, a synthetic real-client/translator round trip on macOS checked all
five effort levels for Terra in both CLI and interactive `/effort` use. The
fake upstream saw the requested level and model; the observed synthetic cache
key, tool definitions and historical input prefix stayed structurally stable
across switches. This is not a provider entitlement, real cache-hit or quality
comparison. Windows interactive terminals were not tested.
