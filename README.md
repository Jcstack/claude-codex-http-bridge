# Claude Code → Codex HTTP bridge

Claude Code remains the **harness**: it owns the terminal UI, permissions, tools, MCP, skills, hooks, subagents, and conversation. A local authenticated HTTP proxy translates its Anthropic Messages requests to Codex model inference. This project does not run Codex CLI as a tool or agent inside Claude Code.

```text
Claude Code ──Anthropic Messages──> 127.0.0.1 + private local key
                                    CLIProxyAPI ──Codex Responses──> OpenAI
```

The repository contains source and an example config only. It does **not** contain the upstream proxy binary, OAuth credentials, API keys, prompts, transcripts, or runtime logs.

## 中文快速开始

Claude Code 是交互界面和工具执行者；Codex 模型由仅监听本机的 HTTP 代理提供。安装后运行：

macOS、Linux、WSL：

```bash
./install.sh
claude-codex codex-login
claude-codex models
claude-codex --model gpt-6.1-sol
```

原生 Windows PowerShell：

```powershell
py -3 .\install.py
# 关闭并重新打开终端后：
claude-codex codex-login
claude-codex models
claude-codex --model gpt-6.1-sol
```

在会话中用 `/model` 切换动态发现的模型，用 `/effort` 调整推理强度。`/background` 需要的本机代理会继续运行；任务结束后可运行 `claude-codex bridge stop`。原有 `claude` 命令仍按原来的账号与设置运行。若本机已有 `claude-codex` 命令，安装器会停止；确认要改用这个仓库的版本后运行 `./install.sh --replace-command`。OAuth 文件和运行日志只留在本机安装目录，不进入 Git。

## Requirements

- macOS, Linux, or native Windows on ARM64/aarch64 or x64
- On Windows: Windows 10 1809+ or Windows Server 2019+ and an NTFS installation directory. Windows Subsystem for Linux uses the Linux instructions.
- Claude Code installed and on `PATH` (verified with Claude Code 2.1.312)
- Python 3.10+; on macOS/Linux/WSL, `bash` and `lsof`
- A ChatGPT account with Codex model access

## Install

macOS/Linux/WSL: run `./install.sh`. Native Windows: run `py -3 .\install.py` in PowerShell or `py -3 install.py` in CMD, then open a new terminal before using `claude-codex`. If `py` is unavailable but Python is installed, use `python`.

```text
claude-codex codex-login
claude-codex models
```

The installer downloads the pinned [CLIProxyAPI v8.0.46 release](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.46), verifies its published SHA-256 digest, and saves it under `~/.local/share/claude-codex-http-bridge/` on macOS/Linux or `%LOCALAPPDATA%\claude-codex-http-bridge\` on Windows. It generates a random local key and a private config on your own machine. `codex-login` opens the OAuth flow and stores the resulting credential only in the installation's private `auth/` directory. The Windows installer adds its private `bin` directory to the current user's PATH; open a new terminal to use the command.

If the `claude-codex` command already exists at the install location, the installer stops without changing it. Use `--replace-command` only when you want the new wrapper to replace that command. It does not copy or remove any existing OAuth credential.

For an isolated installation, pass `--root /private/path --bin-dir /private/path` to `install.sh` on POSIX, or `--root C:\private\path` to `install.py` on Windows.

## Use

```bash
cd /path/to/your/project
claude-codex                         # newest catalog-listed Sol
claude-codex --model gpt-6-sol       # exact model ID
claude-codex --model gpt-5.6-sol --effort max
claude-codex bridge status            # localhost service state
claude-codex bridge stop              # after background work has finished
```

Inside Claude Code:

- `/model` lists every eligible text model returned by the local bridge's current `/v1/models` catalog. Use the arrow keys and press **s** to switch for this session only. Enter saves the model to Claude Code's user settings and may affect a later plain `claude` launch.
- `/effort` selects `low`, `medium`, `high`, `xhigh`, or `max`. `/effort ultracode` enables Claude Code's own dynamic workflows; it is not Codex Ultra.
- `/autocompact` shows the safe threshold for the selected model. The launcher supplies a per-model setting, so `/model` switches also switch the threshold.

The model list, route context windows, compaction thresholds, and status-line context mapping are generated from the authenticated local model catalog at session start. No model IDs or context sizes are fixed in the launcher. Restart `claude-codex` to refresh the picker when the upstream catalog changes. The proxy release itself stays pinned until its checksum and compatibility have been reviewed.

An API model's published maximum can differ from the context advertised by this ChatGPT-plan route. For example, on 2026-10-30 the local catalog advertised `272000` for GPT-6 Astra, while its [API model page](https://developers.openai.com/api/docs/models/gpt-6-astra) listed `1,050,000`; the page's `272K` price threshold does not grant a larger context to this route. The bridge uses the live route value until that catalog changes or a larger entitlement is verified.

### Claude HUD and other status-line plugins

The launcher reads the existing user `statusLine.command` and passes Claude Code a private per-launch settings file that runs that same command through `statusline_adapter.py`. The adapter corrects the context size and percentage for the **current** Codex model, including after `/model` switches. It removes Claude-list-price dollars, Anthropic prompt-cache timers, and Anthropic rate-limit fields from the plugin input because they do not describe this Codex route. Other status-line fields and the user's original command are preserved; the user's settings file is not changed. The per-launch file remains available to detached background sessions and is removed by `claude-codex bridge stop`. Set `CLAUDE_CODEX_STATUSLINE_ADAPTER=0` before launch to disable the adapter.

Plain `claude` remains separate and uses its ordinary provider settings. `claude-codex models` is a catalog listing, not an entitlement guarantee; a completed inference request confirms access to a selected model.

## Tested behavior and boundaries

On macOS Apple Silicon, the bridge was tested with GPT-6.1 Sol, GPT-6 Sol, and GPT-5.6 Sol inference. In one interactive session, `/model` switched GPT-6.1 Sol → GPT-6 Sol → GPT-6.1 Terra and the actual response model changed. `/autocompact` reported 400k for the 500k routes and 200k for the 272k routes. An authenticated `--bg` task completed with a `PONG` response after the launcher exited. Claude Code WebSearch, WebFetch, Read, Edit, Bash, Agent, MCP/ToolSearch, image input, structured output, and streaming were exercised during bridge evaluation; this is a functional sample, not exhaustive certification of every Claude Code release and command. Native Windows CI checks installation, file ACLs, model catalog logic, proxy service lifecycle, and launcher startup without storing any OAuth credential; live Windows inference still requires an authenticated Windows session.

`modelPicker.behavesAs` lets Claude Code offer model capabilities and avoids its `unrecognized_model` diagnostic. Claude Code may therefore display the mapped Claude model's **1M context and list-price cost**. Those UI figures are not the Codex subscription route's limit or bill. The local catalog's `context_window` controls the generated auto-compaction thresholds. See [Claude Code model configuration](https://code.claude.com/docs/en/model-config) and [the per-model settings reference](https://code.claude.com/docs/en/settings-reference#modelsettings).

The localhost proxy starts on first use and is reused by later `claude-codex` sessions, including detached `/background` and `--bg` work. Run `claude-codex bridge status` to inspect it, and `claude-codex bridge stop` after background work has finished; stopping it disconnects any still-running sessions. Claude-account features such as Remote Control have [separate provider requirements](https://code.claude.com/docs/en/remote-control#requirements). OpenAI's ChatGPT-plan third-party inference path has [native tool limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations); model capability is not identical to automatic exposure of every Responses tool through Claude Code.

## Security and privacy

- The proxy binds `127.0.0.1` and requires a random local key stored in the private runtime directory for the service lifetime. The launcher verifies the listener PID before sending the key or reusing the service. On Windows it checks with `netstat`; on POSIX it uses `lsof`.
- On POSIX, OAuth and runtime directories are `0700`; credentials, config, key, and checksum files are `0600`. On Windows, NTFS ACLs restrict installation and runtime files to the current user and are verified before launch. The proxy binary is checked against the installation's verified release hash at each launch.
- Proxy debug logging, request logging, usage statistics, the management panel, and the auto-update panel are disabled. Claude Code itself can still save local transcripts and communicate with services required by its own features.
- The `.gitignore` is a source allowlist. Before pushing changes, inspect staged files and run `gitleaks git --redact .`.

See [SECURITY.md](SECURITY.md) for credential-handling rules.

See [COMPATIBILITY.md](COMPATIBILITY.md) for the Claude Code feature matrix, Claude HUD behavior, current route limits, and a source-backed model comparison.

## License

The wrapper and installer are MIT licensed. CLIProxyAPI is downloaded separately under its own MIT license; its license is retained in the local installation.
