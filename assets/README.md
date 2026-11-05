# Visual assets and demo provenance

The HarnessLift logo and social card are original code-generated vector artwork. They do not combine or reuse provider logos. `logo.svg` is the project mark; `social-preview.svg` is its editable 1280×640 composition, and `social-preview.png` is the corresponding raster asset for GitHub's social-preview setting. The SVG and PNG use separate vector/raster renderers, so font metrics can differ slightly. Fonts are supplied by the local operating system and are not redistributed here. Project artwork and generator source use this repository's MIT license.

## What the walkthrough shows

`walkthrough.gif` is a 30-second sequence of four terminal states recorded from the integrity-locked Claude Code 2.1.316 client:

1. The normal Claude Code terminal in an owned `sample-project` workspace.
2. The real `/model` picker with two explicitly synthetic catalog entries.
3. A change from the Sol entry to the Terra entry, followed by a real local `Read` call. A scripted Responses origin asks Claude Code to read a disposable file; the origin verifies the resulting `SYNTHETIC_READ_OK` tool output.
4. An owned status-line fixture rendered by Claude Code. It is **not Claude HUD** and does not display a real subscription quota, context limit, cost, or cache measurement.

The translator, fake origin, home, configuration, sample file, and status line are disposable. No installed bridge is reused and no real model/account is contacted. The fake Responses origin is reached through a separate loopback translator; the client therefore displays its generic **API Usage Billing** label. That label is not evidence of a billed provider call or a requirement to buy API credits for the subscription route. The client can also display its built-in default-model row; the fake catalog does not prove entitlement to that model.

`demo-screens.json` contains only these synthetic terminal rows and short provenance fields. It contains no real user session recording, account information, credentials, or private paths. The GIF re-renders terminal text into original framing, normalizes three decorative glyphs for font coverage, and holds the four states for readability. It is a protocol walkthrough, not a continuous screen recording, speed comparison, model-quality benchmark, or evidence of full feature parity. The pinned client is provided by Anthropic; the project mark and surrounding framing are HarnessLift's own artwork.

## Reproduce

Use a disposable development environment. The optional wheel-hash lock below is for asset generation only; these packages are not added to the bridge runtime:

```bash
python3 -m venv /tmp/harnesslift-artwork
/tmp/harnesslift-artwork/bin/python -m pip --isolated install --only-binary=:all: --require-hashes -r assets/requirements-demo.txt
npm ci --prefix tests/claude-client --ignore-scripts --no-audit --no-fund
```

On macOS or Linux, supply an independently reviewed translator executable, following [RUNTIME.md](../RUNTIME.md). The original recording used the reviewed `9717e325`-based translator, without the experimental reasoning-retry patch. The recorder uses the test fixture's API translation route with synthetic credentials and a scripted local Responses origin. It starts and stops only its own temporary processes:

```bash
/tmp/harnesslift-artwork/bin/python assets/record_demo.py /path/to/reviewed/cli-proxy-api
/tmp/harnesslift-artwork/bin/python assets/build_brand_assets.py
```

The terminal recorder is POSIX-only; it does not validate Windows interactive behavior. Rendering existing screen text and artwork is portable. Use `--font PATH` and `--mono-font PATH` if the platform's standard fonts are unavailable, or `--artwork-only` to rebuild the logo and social card without a recording. Do not substitute user screenshots or transcript files. Inspect every generated frame before publishing, then run the normal secret scans.
