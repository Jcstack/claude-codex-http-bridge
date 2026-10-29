# Claude Code + Codex compatibility record

Checked 2026-10-29. Claude Code is the harness. The local proxy supplies model inference through the Anthropic Messages interface. A capability is only marked verified when it was exercised; support in one release is not a promise about all future releases or third-party plugins.

| Area | Current result | Evidence or boundary |
| --- | --- | --- |
| Model inference and streaming | Verified on macOS with GPT-6.1 Sol, GPT-6 Sol, and GPT-5.6 Sol. | Real completion requests in the bridge evaluation. The local catalog is not an account entitlement test. |
| `/model` | Verified interactive switching among Sol and Terra routes. | The picker is rebuilt from the authenticated local `/v1/models` catalog at session start. Restart the session for new catalog entries. |
| `/effort` | Low through max are offered only for catalog rows advertising all five levels. | Claude Code's `ultracode` is its own workflow setting, not a Codex reasoning level. |
| `/autocompact` | Verified with per-model safe thresholds computed from live route metadata. | A 500k route produced 400k; a 272k route produced 200k in the observed catalog. These are examples, not constants. |
| Claude HUD and other status-line commands | The temporary adapter corrects the selected model's context size and percentage after `/model` switches. A synthetic run against the user's installed Claude HUD showed `115k/500k` and `115k/272k` instead of `115k/1.0M`. | The adapter withholds Claude-list-price cost, Anthropic prompt-cache timer, and Anthropic rate-limit fields. It leaves the user's plugin command and settings file intact. |
| Read, Edit, Bash, Agent, skills, hooks, local plugins, MCP | These are Claude Code harness features; a sample of tools, ToolSearch, and MCP was exercised on macOS. | [Claude Code's provider matrix](https://code.claude.com/docs/en/feature-availability) lists local subagents, hooks, commands, skills, plugins, and MCP as available across providers. Individual plugins may still assume a Claude account or specific telemetry. |
| WebSearch and WebFetch | Exercised in the bridge evaluation. | Web search depends on the route and account policy. Claude Code's tool is distinct from every OpenAI-hosted Responses tool. |
| Images and structured output | Exercised in the bridge evaluation. | Audio and video are not supported by this ChatGPT-plan inference path. |
| Native Windows | Installer, archive verification, ACLs, dynamic model logic, and command startup are checked in Windows CI. | Live Windows OAuth/model inference still needs an authenticated Windows run. WSL follows the Linux path. |
| Claude Code `/context` and native cost figures | Claude Code can still report the mapped Claude model's 1M window and list-price dollars internally. | The per-model compact window protects the conversation, and the status-line adapter corrects HUD. The CLI's internal `/context`, `/usage`, and budget calculations are not rewritten by this project. |
| Claude-account cloud features | Not supplied by the localhost model endpoint. | Claude Code disables features such as Remote Control and server-managed settings when `ANTHROPIC_BASE_URL` is not Anthropic's API; other cloud features require a claude.ai sign-in. See [feature availability](https://code.claude.com/docs/en/feature-availability). |
| Background `--bg` | Rejected by this session-owned launcher. | A detached Claude Code session would lose its local model proxy when the launcher exits. |
| OpenAI-hosted Responses tools | Only the ChatGPT-plan route's supported tool subset is available through the proxy; they are not automatically added to Claude Code's tool menu. | [OpenAI's plan-usage preview limits](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) allow client function/custom tools and conditional web search, while excluding hosted image generation, file search, Code Interpreter, native computer use, hosted MCP/connectors, and Responses `tool_search`. |

## Model comparison relevant to this bridge

| Aspect | Codex route | Claude models in Claude Code |
| --- | --- | --- |
| Context visible to this installation | The authenticated proxy catalog supplies each route's `context_window`. GPT-6.1 Sol's Codex/ChatGPT Work default is 500k even though its API model page lists a larger API window. | [Claude's current model table](https://platform.claude.com/docs/en/models/overview) lists 1M for Fable 5.1, Opus 5.5, Sonnet 5.5, and Haiku 5.5. |
| Reasoning controls | GPT-6.1 Sol supports `low`, `medium`, `high`, `xhigh`, and `max`. | Claude models use adaptive thinking and model-dependent default effort. Similar effort names do not imply equal reasoning work. |
| Documented positioning | OpenAI positions Astra for its most demanding tasks, GPT-6.1 Sol for complex work balanced against time and cost, Terra for visual/computer-use work, and Luna for efficient scoped work. | Anthropic positions Fable for demanding long-horizon reasoning, Opus for long-running coding/knowledge work, Sonnet for speed and intelligence, and Haiku for high-throughput tasks. |
| Billing display | A ChatGPT-plan route's quota and bill are not Claude list-price dollars. This bridge hides those dollars from the external status line. | Claude Code can calculate list-price or managed contract-rate figures for supported Claude providers. |

Sources: [GPT-6.1 Sol model page](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [OpenAI model selection](https://developers.openai.com/api/docs/guides/model-selection), [Claude model overview](https://platform.claude.com/docs/en/models/overview), [Claude Code model configuration](https://code.claude.com/docs/en/model-config), and [status-line input schema](https://code.claude.com/docs/en/statusline).

There is no controlled head-to-head quality result here. To compare models for a specific repository, use the same task, effort budget, tools, and acceptance tests, then inspect the resulting code and time to completion. The bridge's compatibility alias is a Claude Code UI/capability mapping; it does not change the underlying Codex model.
