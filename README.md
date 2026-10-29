# Claude Code → Codex HTTP bridge

Claude Code remains the **harness**: it owns the terminal UI, permissions, tools, MCP, skills, hooks, subagents, and conversation. A local authenticated HTTP proxy translates its Anthropic Messages requests to Codex model inference. This project does not run Codex CLI as a tool or agent inside Claude Code.

```text
Claude Code ──Anthropic Messages──> 127.0.0.1 + session key
                                    CLIProxyAPI ──Codex Responses──> OpenAI
```

The repository contains source and an example config only. It does **not** contain the upstream proxy binary, OAuth credentials, API keys, prompts, transcripts, or runtime logs.

## 中文快速开始

Claude Code 是交互界面和工具执行者；Codex 模型由仅监听本机的 HTTP 代理提供。安装后运行：

```bash
./install.sh
claude-codex codex-login
claude-codex models
claude-codex --model gpt-6.1-sol
```

在会话中用 `/model` 切换动态发现的模型，用 `/effort` 调整推理强度。原有 `claude` 命令仍按原来的账号与设置运行。若本机已有 `claude-codex` 命令，安装器会停止；确认要改用这个仓库的版本后运行 `./install.sh --replace-command`。OAuth 文件和运行日志只留在本机安装目录，不进入 Git。

## Requirements

- macOS or Linux on arm64/aarch64 or x86_64
- Claude Code installed and on `PATH` (verified with Claude Code 2.1.312)
- Python 3.10+, `bash`, `lsof`, and `nc`
- A ChatGPT account with Codex model access

## Install

```bash
./install.sh
claude-codex codex-login
claude-codex models
```

The installer downloads the pinned [CLIProxyAPI v8.0.46 release](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.46), verifies its published SHA-256 digest, and saves it under `~/.local/share/claude-codex-http-bridge/`. It generates a random local key and a private config on your own machine. `codex-login` opens the OAuth flow and stores the resulting credential only in the installation's private `auth/` directory.

If `~/.local/bin/claude-codex` already exists, the installer stops without changing it. Use `./install.sh --replace-command` only when you want the newly installed wrapper to replace that command. It does not copy or remove any existing OAuth credential.

For an isolated installation, pass `--root /private/path --bin-dir /private/path` to `install.sh`.

## Use

```bash
cd /path/to/your/project
claude-codex                         # newest catalog-listed Sol
claude-codex --model gpt-6-sol       # exact model ID
claude-codex --model gpt-5.6-sol --effort max
```

Inside Claude Code:

- `/model` lists every eligible text model returned by the local bridge's current `/v1/models` catalog. Use the arrow keys and press **s** to switch for this session only. Enter saves the model to Claude Code's user settings and may affect a later plain `claude` launch.
- `/effort` selects `low`, `medium`, `high`, `xhigh`, or `max`. `/effort ultracode` enables Claude Code's own dynamic workflows; it is not Codex Ultra.
- `/autocompact` shows the safe threshold for the selected model. The launcher supplies a per-model setting, so `/model` switches also switch the threshold.

Plain `claude` remains separate and uses its ordinary provider settings. `claude-codex models` is a catalog listing, not an entitlement guarantee; a completed inference request confirms access to a selected model.

## Tested behavior and boundaries

On macOS Apple Silicon, the wrapper was tested with GPT-6.1 Sol, GPT-6 Sol, and GPT-5.6 Sol inference. In one interactive session, `/model` switched GPT-6.1 Sol → GPT-6 Sol → GPT-6.1 Terra and the actual response model changed. `/autocompact` reported 400k for the 500k routes and 200k for the 272k routes. Claude Code WebSearch, WebFetch, Read, Edit, Bash, Agent, MCP/ToolSearch, image input, structured output, and streaming were exercised during bridge evaluation; this is a functional sample, not exhaustive certification of every Claude Code release and command.

`modelPicker.behavesAs` lets Claude Code offer model capabilities and avoids its `unrecognized_model` diagnostic. Claude Code may therefore display the mapped Claude model's **1M context and list-price cost**. Those UI figures are not the Codex subscription route's limit or bill. The local catalog's `context_window` controls the generated auto-compaction thresholds. See [Claude Code model configuration](https://code.claude.com/docs/en/model-config) and [the per-model settings reference](https://code.claude.com/docs/en/settings-reference#modelsettings).

The proxy is owned by the foreground launcher and stops when it exits. `--bg`/`--background` is intentionally rejected because the detached Claude Code session would lose its model endpoint. Claude-account features such as Remote Control have [separate provider requirements](https://code.claude.com/docs/en/remote-control#requirements). OpenAI's ChatGPT-plan third-party inference path has [native tool limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations); model capability is not identical to automatic exposure of every Responses tool through Claude Code.

## Security and privacy

- The proxy binds `127.0.0.1` and requires a random key that exists only in a `0700` session directory while Claude Code runs. The launcher verifies the listener belongs to the child proxy process before sending the key.
- The installed OAuth directory is `0700`; credentials, config, and checksum files are `0600`. The proxy binary is checked against the installation's verified release hash at each launch.
- Proxy debug logging, request logging, usage statistics, the management panel, and the auto-update panel are disabled. Claude Code itself can still save local transcripts and communicate with services required by its own features.
- The `.gitignore` is a source allowlist. Before pushing changes, inspect staged files and run `gitleaks git --redact .`.

See [SECURITY.md](SECURITY.md) for credential-handling rules.

## License

The wrapper and installer are MIT licensed. CLIProxyAPI is downloaded separately under its own MIT license; its license is retained in the local installation.
