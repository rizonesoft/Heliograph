#!/usr/bin/env python3
"""Render Heliograph's images from their sources: python3 tools/brand.py --os <Impossible OS checkout> --font <font>

brand/icon/ holds the sources: icon.svg for 48 pixels and up, and a drawing of its own for 16, 20, 24 and 32.
This writes the PNGs, icon.ico, the wordmarks, the README banners, the social preview and the badge.

Needs rsvg-convert, ImageMagick, fontTools and a Chromium (CHROME, or Playwright's). The interface and code fonts
are read from the checkout's resources; --font is Plus Jakarta Sans (OFL), the variable file of Google Fonts.
"""
import argparse
import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "brand"
SMALL, LARGE = (16, 20, 24, 32), (48, 64, 72, 96, 128, 256)
TAG = "Messages and jobs between agent sessions, on Windows and in WSL. Every job has one taker."
ID = "1795472530719051-5faf73"


def source(size):
    return BRAND / "icon" / (f"icon-{size}.svg" if size in SMALL else "icon.svg")


def number(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


class Type:
    def __init__(self, path):
        from fontTools.ttLib import TTFont
        from fontTools.varLib.instancer import instantiateVariableFont
        self.font = instantiateVariableFont(TTFont(path), {"wght": 700})
        self.glyphs, self.cmap, self.upm = self.font.getGlyphSet(), self.font.getBestCmap(), self.font["head"].unitsPerEm
        self.cap = self.font["OS/2"].sCapHeight

    def width(self, text, size, track=-.012):
        return sum(self.glyphs[self.cmap[ord(c)]].width / self.upm + track for c in text) * size

    def path(self, text, x, base, size, track=-.012):
        from fontTools.pens.svgPathPen import SVGPathPen
        from fontTools.pens.transformPen import TransformPen
        out, k = "", size / self.upm
        for c in text:
            pen = SVGPathPen(self.glyphs, ntos=lambda v: number(v))
            self.glyphs[self.cmap[ord(c)]].draw(TransformPen(pen, (k, 0, 0, -k, x, base)))
            out, x = out + pen.getCommands(), x + (self.glyphs[self.cmap[ord(c)]].width / self.upm + track) * size
        return out


def ink(light):
    return "#1B1B1B" if light else "#F5F8FF"


def inner(text):
    return text.split(">", 1)[1].rsplit("</svg>", 1)[0]


def wordmark(font, light):
    """The name drawn, as (svg, width, height). Outlines only, so it needs no font."""
    size, base, pad = 120, 128, 12
    cap, color = font.cap / font.upm * size, ink(light)
    mark = cap * 48 / 40  # the mark is the H
    body = f'<g transform="translate({pad - mark * 4 / 48:.2f} {base - cap - mark * 4 / 48:.2f}) scale({mark / 48:.4f})">{inner(source(48).read_text(encoding="utf-8"))}</g>'
    x = pad + cap + size * .095
    body += f'<path d="{font.path("eliograph", x, base, size)}" fill="{color}"/>'
    return done(body, x + font.width("eliograph", size) + pad, 170)


def done(body, width, height):
    w = int(width + .99)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {height}" width="{w}" height="{height}">' + body + "</svg>\n", w, height


def picture(path, width, height):
    """An SVG as a picture: the browser runs nothing inside it."""
    return f"<img src='{path.as_uri()}' width='{width:g}' height='{height:g}' style='display:block' alt=''>"


def icon(size):
    return picture(source(size), size, size)


def mark(light, height):
    path = BRAND / f"wordmark-{'light' if light else 'dark'}.svg"
    w, h = (int(v) for v in path.read_text(encoding="utf-8").split('viewBox="0 0 ', 1)[1].split('"', 1)[0].split())
    return picture(path, round(w * height / h, 1), height)


def ico():
    """The kit's own ICO writer, so the icon and the launchers that read it agree."""
    spec = importlib.util.spec_from_file_location("campaign_icon", ROOT / "scripts/campaign-icon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def theme(light):
    return dict(ink="#1B1B1B", quiet="#5D5D5D", back="rgba(249,249,251,.95)", title="rgba(238,242,248,.96)", edge="rgba(0,0,0,.1)", key="#005FB8", gold="#8A5A00", red="#B3261E", shade=".2") if light else \
        dict(ink="#F2F2F2", quiet="#A9AFBA", back="rgba(16,18,24,.94)", title="rgba(36,39,47,.96)", edge="rgba(255,255,255,.12)", key="#60CDFF", gold="#FFD75E", red="#FF99A4", shade=".55")


def terminal(light, box, label, lines, font=12.2):
    """A window of Windows 11's shape with what the program prints."""
    t, (x, y, w, h) = theme(light), box
    z = font / 12.2
    glyphs = "".join(f"<svg width='{9 * z:.1f}' height='{9 * z:.1f}' viewBox='0 0 10 10'>{s}</svg>" for s in ("<path d='M0 5h10' stroke='currentColor'/>", "<rect x='.5' y='.5' width='9' height='9' rx='1.4' fill='none' stroke='currentColor'/>", "<path d='M0 0l10 10M10 0L0 10' stroke='currentColor'/>"))
    return (f"<div style='position:absolute;left:{x}px;top:{y}px;width:{w}px;height:{h}px;border-radius:8px;overflow:hidden;background:{t['back']};border:1px solid {t['edge']};box-shadow:0 16px 40px rgba(0,8,36,{t['shade']});color:{t['ink']}'>"
            f"<div style='height:{32 * z:.0f}px;display:flex;align-items:center;gap:{8 * z:.0f}px;padding:0 {14 * z:.0f}px 0 {11 * z:.0f}px;background:{t['title']};font-size:{12 * z:.1f}px'>{icon(16)}{label}<span style='margin-left:auto;display:flex;gap:{22 * z:.0f}px;color:{t['quiet']}'>{glyphs}</span></div>"
            f"<div style='padding:{12 * z:.0f}px {15 * z:.0f}px;font:{font}px/1.62 Cascadia;white-space:pre-wrap;overflow-wrap:anywhere'>{lines}</div></div>")


def talk(light):
    t = theme(light)
    return lambda kind, text: f"<span style='color:{t[kind]}'>{text}</span>"


def scene(light, social):
    """Two sessions and the beam between them: the mark, made of windows."""
    s = talk(light)
    x1, x2, y, w, h, z = (80, 660, 344, 540, 248, 1.36) if social else (560, 918, 62, 314, 236, .93)
    one = f"{s('quiet', '&gt;')} {s('key', 'helio')} take {ID}\n{s('quiet', '&gt;')} {s('key', 'helio')} done {ID}\n{s('quiet', '&gt;')} "
    two = f"{s('quiet', '$')} {s('key', 'helio')} take {ID}\nhelio: it is {s('gold', 'already taken')}\n{s('quiet', '$')} "
    tall, top = 44 if social else 36, y + h * .62
    over, r = w * .22, tall / 2  # how far the beam lies over the first window
    long = over + x2 - x1 - w + w * .3
    end = f"{long - r:.1f}"
    beam = (f"<svg style='position:absolute;left:{x1 + w - over:.1f}px;top:{top:.1f}px;overflow:visible' width='{long:.0f}' height='{tall}'><defs>"
            "<linearGradient id='hb' x1='0' x2='0' y1='0' y2='1'><stop offset='0' stop-color='#9CEBFC' stop-opacity='.9'/><stop offset='.5' stop-color='#62DDF9' stop-opacity='.9'/><stop offset='1' stop-color='#3CC3F5' stop-opacity='.9'/></linearGradient>"
            "<radialGradient id='hc'><stop offset='0' stop-color='#FFF'/><stop offset='.5' stop-color='#FFF' stop-opacity='.85'/><stop offset='1' stop-color='#FFF' stop-opacity='0'/></radialGradient></defs>"
            f"<rect width='{long:.1f}' height='{tall}' rx='{r}' fill='url(#hb)'/><path d='M{r} .5H{end}' stroke='#FFF' stroke-opacity='.7'/><circle cx='{end}' cy='{r}' r='{tall * .46:.1f}' fill='url(#hc)'/><circle cx='{end}' cy='{r}' r='{tall * .22:.1f}' fill='#FFF'/></svg>")
    return terminal(light, (x1, y, w, h), "Windows: impossible/apps", one, 12 * z) + terminal(light, (x2, y, w, h), "WSL: impossible/userland", two, 12 * z) + beam


def ground(light):
    """Mica: one calm tint with the two sessions' blues."""
    return ("radial-gradient(60% 120% at 46% 0%,rgba(92,230,247,.42),transparent 70%),radial-gradient(60% 120% at 100% 100%,rgba(46,125,235,.38),transparent 70%),linear-gradient(#F3F7FC,#EAF1FA)" if light else
            "radial-gradient(60% 120% at 46% 0%,rgba(46,181,240,.3),transparent 70%),radial-gradient(60% 120% at 100% 100%,rgba(21,92,222,.55),transparent 70%),linear-gradient(#0B1324,#080E1C)")


def banner(light, social):
    soft = "#3f4754" if light else "#C9D6EA"
    words = f"<div style='font-size:{27 if social else 20}px;line-height:1.38;color:{soft};max-width:{760 if social else 470}px'>{TAG}</div>"
    place = "left:80px;top:70px" if social else "left:56px;top:0;bottom:0;justify-content:center"
    return (f"<div style='position:absolute;inset:0;background:{ground(light)}'></div>{scene(light, social)}"
            f"<div style='position:absolute;{place};display:flex;flex-direction:column;gap:{20 if social else 14}px'><div style='margin-left:-8px'>{mark(light, 132 if social else 106)}</div>{words}</div>")


def badge(light):
    back, color, edge = ("#FFFFFF", "#1B1B1B", "#D5D9DF") if light else ("#2B2B2B", "#FFFFFF", "#474747")
    return (f"<span style='display:inline-flex;align-items:center;gap:8px;height:28px;padding:0 11px 0 9px;border:1px solid {edge};border-radius:4px;background:{back};color:{color};font-weight:600;font-size:12.5px;white-space:nowrap'>{icon(16)}Messages by Heliograph</span>")


def page(resources, body, width, height, trim):
    fonts = resources / "fonts"
    size = "display:inline-block;padding:16px" if trim else f"width:{width}px;height:{height}px;overflow:hidden"
    return (f"<!doctype html><meta charset='utf-8'><style>@font-face{{font-family:Selawik;src:url({fonts.as_uri()}/selawk.ttf)}}@font-face{{font-family:Selawik;font-weight:600;src:url({fonts.as_uri()}/selawksb.ttf)}}"
            f"@font-face{{font-family:Cascadia;src:url({fonts.as_uri()}/CascadiaCode-Regular.ttf)}}*{{box-sizing:border-box}}body{{margin:0;position:relative;font-family:Selawik;background:transparent;{size}}}</style><body>{body}")


def chromium():
    found = sorted((Path.home() / ".cache/ms-playwright").glob("chromium-*/chrome-linux*/chrome"), key=lambda p: int(p.parts[-3].split("-")[1]))
    given = os.environ.get("CHROME") or (found and str(found[-1]))
    if not given:
        raise SystemExit("no Chromium found; set CHROME")
    return given


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--os", type=Path, required=True, help="a checkout of Impossible OS, for its fonts")
    parser.add_argument("--font", type=Path, required=True)
    given = parser.parse_args()
    resources, font, chrome, png = (given.os / "resources").resolve(), Type(given.font), chromium(), BRAND / "png"
    for name in ("selawk.ttf", "selawksb.ttf", "CascadiaCode-Regular.ttf"):
        if not (resources / "fonts" / name).is_file():
            parser.error(f"{resources} does not have the font {name}")
    png.mkdir(exist_ok=True)
    for size in SMALL + LARGE:
        subprocess.run(["rsvg-convert", "-w", str(size), source(size), "-o", png / f"icon-{size}.png"], check=True, timeout=60)
    kit = ico()
    kit.write_atomic(BRAND / "icon.ico", kit.pack_ico([(s, (png / f"icon-{s}.png").read_bytes()) for s in (*SMALL, 48, 64, 256)]))
    with tempfile.TemporaryDirectory() as folder:
        def shot(name, body, width, height, scale, trim):
            html, out = Path(folder, "page.html"), BRAND / f"{name}.png"
            html.write_text(page(resources, body, width, height, trim), encoding="utf-8")
            subprocess.run([chrome, "--headless=new", "--no-sandbox", "--hide-scrollbars", "--allow-file-access-from-files", "--virtual-time-budget=3000", "--default-background-color=00000000",
                            f"--force-device-scale-factor={scale}", f"--window-size={width},{height}", f"--screenshot={out}", html.as_uri()], check=True, capture_output=True, timeout=120)
            subprocess.run(["convert", out, *(["-trim", "+repage", "-bordercolor", "none", "-border", "8"] if trim else []), "-strip", out], check=True, timeout=60)
        for light in (False, True):
            on = "light" if light else "dark"
            (BRAND / f"wordmark-{on}.svg").write_text(wordmark(font, light)[0], encoding="utf-8")
            subprocess.run(["rsvg-convert", "-z", "2", BRAND / f"wordmark-{on}.svg", "-o", BRAND / f"wordmark-{on}.png"], check=True, timeout=60)
            shot(f"banner-{on}", banner(light, False), 1280, 360, 2, False)
            shot(f"badge-{on}", badge(light), 400, 120, 2, True)
        shot("social-preview", banner(False, True), 1280, 640, 1, False)


if __name__ == "__main__":
    main()
