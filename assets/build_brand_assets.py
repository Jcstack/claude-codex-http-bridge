"""Render original HarnessLift artwork and a labelled synthetic walkthrough.

Only checked-in synthetic screen text is read. No user screenshots, accounts,
transcripts, browser profiles, or installed bridge directories are accessed.
"""

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
BG = "#101923"
PANEL = "#192632"
INK = "#edf2f6"
MUTED = "#9eb0bf"
MINT = "#8be0c2"
CORAL = "#ffad8e"
LOGO = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" role="img" aria-label="HarnessLift">
  <rect width="128" height="128" rx="30" fill="#101923"/>
  <g fill="none" stroke="#8be0c2" stroke-width="10" stroke-linecap="round" stroke-linejoin="round">
    <path d="M32 94V36M32 66H88M88 94V23"/>
    <path d="M69 42L88 23L107 42" stroke="#ffad8e"/>
  </g>
</svg>
'''


def font(size, *, bold=False, mono=False, selected=None):
    candidates = ([str(selected)] if selected else []) + (
        ["/System/Library/Fonts/Menlo.ttc", "C:/Windows/Fonts/consola.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"] if mono else
        (["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf",
          "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else
         ["/System/Library/Fonts/Supplemental/Arial.ttf", "C:/Windows/Fonts/arial.ttf",
          "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]))
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    raise RuntimeError("No suitable font; supply --font and --mono-font")


def icon(draw, x, y, scale=1):
    def points(values):
        return [(x + a * scale, y + b * scale) for a, b in values]
    draw.rounded_rectangle((x, y, x + 128 * scale, y + 128 * scale), radius=30 * scale, fill=BG)
    for values, color in [([(32, 94), (32, 36)], MINT), ([(32, 66), (88, 66)], MINT),
                          ([(88, 94), (88, 23)], MINT), ([(69, 42), (88, 23), (107, 42)], CORAL)]:
        draw.line(points(values), fill=color, width=round(10 * scale), joint="curve")
        for a, b in values:
            radius = 5 * scale
            cx, cy = x + a * scale, y + b * scale
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=color)


def social(selected):
    image = Image.new("RGB", (1280, 640), BG)
    draw = ImageDraw.Draw(image)
    icon(draw, 60, 54, 1)
    draw.text((222, 67), "HarnessLift", font=font(68, bold=True, selected=selected), fill=INK)
    draw.text((226, 151), "Your harness. Your models.", font=font(25, selected=selected), fill=MINT)
    draw.text((72, 254), "Run Claude Code with", font=font(50, bold=True, selected=selected), fill=INK)
    draw.text((72, 315), "your Codex models.", font=font(50, bold=True, selected=selected), fill=INK)
    labels = [(72, "Claude Code", "Local tools + permissions"),
              (466, "Local bridge", "Private HTTP translation"),
              (860, "Codex models", "Inference via OpenAI")]
    for x, title, subtitle in labels:
        draw.rounded_rectangle((x, 424, x + 348, 525), radius=18, fill=PANEL)
        draw.text((x + 23, 439), title, font=font(28, bold=True, selected=selected), fill=MINT)
        draw.text((x + 23, 482), subtitle, font=font(20, selected=selected), fill=MUTED)
    for x in (434, 828):
        draw.line((x, 470, x + 17, 470), width=3, fill=CORAL)
        draw.line((x + 11, 464, x + 17, 470, x + 11, 476), width=3, fill=CORAL)
    draw.text((72, 565), "DEVELOPER PREVIEW  /  macOS · Linux · Windows", font=font(22, selected=selected), fill=MUTED)
    image.save(ROOT / "social-preview.png", optimize=True)
    text = []
    def label(x, y, value, size, color, weight="400"):
        text.append(f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" font-weight="{weight}">{escape(value)}</text>')
    label(222, 124, "HarnessLift", 68, INK, "700")
    label(226, 176, "Your harness. Your models.", 25, MINT)
    label(72, 298, "Run Claude Code with", 50, INK, "700")
    label(72, 359, "your Codex models.", 50, INK, "700")
    for x, title, subtitle in labels:
        text.append(f'<rect x="{x}" y="424" width="348" height="101" rx="18" fill="{PANEL}"/>')
        label(x + 23, 467, title, 28, MINT, "700")
        label(x + 23, 503, subtitle, 20, MUTED)
    for x in (434, 828):
        text.append(f'<path d="M{x} 470h17m-6-6 6 6-6 6" fill="none" stroke="{CORAL}" stroke-width="3"/>')
    label(72, 587, "DEVELOPER PREVIEW  /  macOS · Linux · Windows", 22, MUTED)
    inner = LOGO[LOGO.index("  <rect"):LOGO.index("</svg>")]
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="640" viewBox="0 0 1280 640" '
           'role="img" aria-label="HarnessLift: run Claude Code with your Codex models">\n'
           f'<rect width="1280" height="640" fill="{BG}"/>\n'
           f'<g transform="translate(60 54)">{inner}</g>\n'
           '<g font-family="Arial, Helvetica, sans-serif">\n' + "\n".join(text) + '\n</g>\n</svg>\n')
    (ROOT / "social-preview.svg").write_text(svg, encoding="utf-8")
    if (ROOT / "social-preview.png").stat().st_size >= 1024 * 1024:
        raise RuntimeError("Social preview exceeds GitHub's image limit")


def walkthrough(selected, mono_selected):
    value = json.loads((ROOT / "demo-screens.json").read_text(encoding="utf-8"))
    if value.get("synthetic_only") is not True or value.get("provider_calls") != 0:
        raise RuntimeError("Only the owned synthetic recording can be rendered")
    mono = font(16, mono=True, selected=mono_selected)
    frames, durations = [], []
    for index, shot in enumerate(value["screens"]):
        image = Image.new("RGB", (1184, 790), BG)
        draw = ImageDraw.Draw(image)
        draw.text((38, 23), "HarnessLift", font=font(28, bold=True, selected=selected), fill=INK)
        draw.text((38, 68), shot["title"], font=font(29, bold=True, selected=selected), fill=MINT)
        draw.text((38, 111), "REAL CLAUDE CODE CLIENT · SCRIPTED LOCAL MODEL · NO PROVIDER CALLS",
                  font=font(16, selected=selected), fill=CORAL)
        draw.rounded_rectangle((26, 157, 1158, 718), radius=14, fill="#111416", outline="#324453", width=1)
        for column, color in enumerate(("#ff8d82", "#eac87e", MINT)):
            x = 46 + column * 21
            draw.ellipse((x, 175, x + 9, 184), fill=color)
        draw.text((132, 169), "sample-project — synthetic protocol demo", font=font(16, selected=selected), fill=MUTED)
        for row, line in enumerate(shot["lines"][:25]):
            if len(line) > 112:
                raise RuntimeError("Recording exceeds the owned terminal width")
            # Pillow has no terminal font fallback. Normalize decoration only;
            # keep the original captured text in demo-screens.json.
            rendered = line.translate(str.maketrans({"⏺": "●", "⎿": "└", "⏵": "▸"}))
            draw.text((46, 205 + row * 20), rendered, font=mono, fill=INK)
        draw.text((38, 745), "Developer preview · Optional features and limits: github.com/Jcstack/harnesslift",
                  font=font(17, selected=selected), fill=MUTED)
        for step in range(len(value["screens"])):
            x = 1038 + 25 * step
            draw.rounded_rectangle((x, 42, x + 15, 47), radius=2, fill=MINT if step == index else "#3c4c5c")
        frames.append(image.quantize(colors=128))
        durations.append(shot["duration_ms"])
    if sum(durations) != 30_000:
        raise RuntimeError("The walkthrough must total 30 seconds")
    frames[0].save(ROOT / "walkthrough.gif", save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True, disposal=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path)
    parser.add_argument("--mono-font", type=Path)
    parser.add_argument("--artwork-only", action="store_true")
    args = parser.parse_args()
    (ROOT / "logo.svg").write_text(LOGO, encoding="utf-8")
    social(args.font)
    if not args.artwork_only:
        walkthrough(args.font, args.mono_font)
    print("Rendered original brand assets" + ("" if args.artwork_only else " and synthetic walkthrough"))


if __name__ == "__main__":
    main()
