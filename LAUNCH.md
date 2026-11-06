# HarnessLift adoption plan

Maintainer working document. Sources checked on 2026-11-06. Posting destinations and rules must be rechecked immediately before use. Drafts below have not been posted.

## Objective

Find developers who want Claude Code's local workflow with Codex-backed models, help them complete a small supervised trial, and learn what prevents continued use. The first experiment seeks five voluntary first-run reports over seven days after an approved community introduction. This is a learning target, not a forecast or a promise of stars.

Repository naming, Topics, artwork, and a release make an introduction easier to understand. Distribution still requires reaching relevant users, and adoption requires a working first run. Treat discovery, trial success, and continued use as separate questions.

## Positioning and evidence

Use this description: **Codex-backed OpenAI models as Claude Code's model provider, through a local HTTP bridge.**

The intended audience already wants Claude Code's terminal, local permissions, tools, MCP, hooks, or project workflow while using Codex-backed inference. Explain the route and relevant compatibility evidence before listing optional capabilities.

| Approach | What the user is choosing |
| --- | --- |
| Native Codex CLI | Codex's own coding harness and its model route |
| A Codex agent exposed through MCP | A separate agent executing delegated work |
| HarnessLift | Claude Code executing the local workflow, with model requests translated to Codex-backed inference |

Other model bridges exist. [Claudex](https://github.com/BeamoTech/Claudex) describes a similar use case and simpler installation channels; its existence means basic bridging is not an exclusive feature. Its [issue tracker](https://github.com/BeamoTech/Claudex/issues) also contains Windows installation and login reports. These are user reports about that project, not a benchmark or proof that HarnessLift avoids every equivalent problem.

The [Multi-CLI author's community post](https://www.reddit.com/r/ClaudeCode/comments/1whw82q/use_any_subscription_in_claude_code_using_the_new/) also presents a plugin-based provider approach. Its implementation and claimed provider-policy implications have not been verified here. Evaluate such alternatives before claiming HarnessLift is the only way to keep Claude Code as the harness.

Potential reasons to try HarnessLift are its documented model/effort/context adaptation, account staging, status-line metadata, native Windows installer checks, and explicit compatibility investigations. Each claim must retain its route and runtime requirements. Do not describe these as proven unique advantages without a direct comparison.

Avoid claims of free Claude inference, unlimited quota, official provider endorsement, complete plugin compatibility, Opus-equivalent quality, or safe unattended recovery. The [same-use-case HN discussion](https://news.ycombinator.com/item?id=47562475) shows that readers can question model identity and account implications. Comments are evidence of reader concerns, not authoritative provider policy or legal advice.

## What can be shown today

- Public source and [preview release](https://github.com/Jcstack/harnesslift/releases/tag/v0.1.0-preview.1), with installation instructions and a feature matrix.
- The completed [three-platform CI run](https://github.com/Jcstack/harnesslift/actions/runs/42064683619), including Linux/Windows installation and command checks. CI does not establish real account entitlement or provider quality.
- The [synthetic real-client walkthrough](assets/README.md). Keep its scripted-model label visible. It is not evidence of actual model speed or task quality.
- A short [first-run feedback form](https://github.com/Jcstack/harnesslift/issues/new?template=trial_feedback.yml). Successful and blocked trials are both useful.

A stronger future demonstration should use a real provider on a disposable, entirely synthetic coding task: show the chosen model, local edits, a test result, and continuation. Identify the route, client, runtime, and relevant setup. Review every frame before publishing; exclude account details, private paths, quota panels, unrelated windows, prompts from real work, and raw logs. Do not record a user's active business session for promotion. That demonstration has not been produced as part of this plan.

The current preview still has unresolved stream interruptions. Early testers should understand the supervision requirement. Broader promotion should follow successful outside trials and measured reliability improvements rather than an expanded promise of universal support.

## Distribution order

| Priority | Destination | Intended contribution and boundary |
| --- | --- | --- |
| 1 | [r/codex weekly Showcase checked on Nov 6](https://www.reddit.com/r/codex/comments/1xmyhim/show_us_all_what_youve_been_building_with_codex/) | One owner-approved comment about the workflow, a direct repo link, and a specific feedback request. Verify that this remains the appropriate current thread. |
| 2 | [r/ClaudeCode weekly Showcase checked on Nov 6](https://www.reddit.com/r/ClaudeCode/comments/1xlurut/weekly_showcase_thread_what_are_you_building_with/) | A separate, relevant account of how Claude Code was involved as the integration client and harness. Do not invent a story that Claude models authored the project. The thread directs short promotion here and prohibits repeated spam and referral links. |
| 3 | [Show HN](https://news.ycombinator.com/showhn.html) | Later, once the maintainer can demonstrate a real first run and answer implementation questions. HN's [guidelines](https://news.ycombinator.com/newsguidelines.html) require human-written contributions and prohibit automated posting. Use the fact sheet below as research; the maintainer writes and posts their own submission. |

Do not post these materials, contact moderators, send DMs, or submit directory/awesome-list PRs without the owner's explicit instruction for the destination and content. Useful upstream contributions should stand on their technical value; an unrelated issue is not an advertising slot. Recheck community rules before any authorized publication. Do not buy stars, solicit coordinated votes, promise rewards for stars, or repeatedly cross-post the same text.

## Draft for the r/codex Showcase

Owner review is required. This is a draft for the Showcase comment above, not an HN submission.

> I wanted to keep Claude Code as my coding harness while using Codex-backed models, so I maintain HarnessLift: https://github.com/Jcstack/harnesslift
>
> It runs a local HTTP bridge: Claude Code handles the terminal, local tool permissions, MCP and project workflow; Codex/OpenAI supplies the model responses. The bridge builds on CLIProxyAPI. Credentials stay in the private local installation, while model requests and their context still go to OpenAI.
>
> Built with Codex and exercised against the real Claude Code client. The preview includes model-picker adaptation and installer checks on macOS, Linux and Windows. Optional native capabilities have separate setup and compatibility limits. The repo's GIF uses a scripted local model and is labelled accordingly.
>
> This is an MIT-licensed developer preview. Stream interruptions remain unresolved, so I am looking for feedback on small supervised tasks rather than recommending unattended runs. You need your own account access to the model provider.
>
> If this is a workflow you want, which step works or blocks you: installation, choosing a model, local tool calls, or MCP? There is a short first-run feedback form in the README. Please keep credentials, account information, private prompts and logs out of public reports.

## Human author fact sheet for HN

Do not copy this document or an AI-written draft into an HN post. The maintainer should write their own account of:

- The actual workflow they wanted and why the native harness or an MCP agent did not meet that preference.
- Which component executes tools, which provider supplies responses, and what the local translator changes.
- What can be installed today, how to try it, and which evidence is synthetic versus real-provider observation.
- Why the project is worth examining alongside existing bridges, with concrete implementation details and credited upstream work.
- Current interruption, compatibility and account prerequisites, without unsupported policy or quality guarantees.

## Seven-day experiment

Day 0 starts with the first explicitly approved community introduction. Recheck the current thread and rules; publish once, then remain available for real questions. The owner must approve any agent-posted content. HN remains a human-authored, human-posted channel under its own rules.

| Period | Action | Evidence to keep |
| --- | --- | --- |
| Day 0 | Capture the available traffic baseline privately; publish the approved first community entry | Post URL and time, no private account or analytics data in Git |
| Days 1–2 | Help interested users through a small supervised trial | Voluntary platform/version/outcome reports; no credentials, prompts, transcripts or logs |
| Days 3–4 | Fix the most common observed blocker; add a regression case if behavior changes | Reproduction with synthetic data and a concrete patch |
| Days 4–5 | If useful and permitted, share a distinct technical account in the second community | A new contribution and current rules, not a duplicate promotion |
| Days 6–7 | Review discovery and reported trial outcomes; decide whether to improve setup, reliability, positioning or distribution | A small evidence-based follow-up, with no promise of a particular star count |

Interpret results cautiously. An internal target is five voluntary first-run reports; reaching a round number of visits or stars is not acceptance. If interested people cannot start the tool, prioritize setup. If trials fail during work, prioritize reliability. If there are few relevant visitors, test a more relevant introduction before drawing a conclusion about demand. These are diagnostic rules for the experiment, not conclusions from the current small sample.

## Measurement and privacy

Use GitHub's existing owner traffic view and APIs. Keep snapshots of views, unique-visitor counts, clones and referrers outside this public repository. The traffic window and refresh behavior limit comparisons; preserve the returned dates and capture time. Private owner analytics are not public marketing proof.

Clone counts are not install or activation counts. Automation and repeated clones can affect interpretation, and the aggregate API does not identify a clone's purpose. Substantial CI activity is a reason to investigate attribution rather than credit every clone to a new user; it does not prove that a particular clone came from CI. Star counts do not show whether anyone completed a task. Record successful trials and blockers only when users voluntarily report them, and label those reports as a non-representative sample. See the [GitHub traffic API](https://docs.github.com/en/rest/metrics/traffic) for the actual metrics and returned time windows.

Do not add tracking pixels, machine identifiers, credential collection, prompt upload, hidden installation telemetry, external analytics services, or scheduled jobs for this experiment. Use direct links; do not claim GitHub exposes per-campaign conversion data that its traffic API does not provide.
