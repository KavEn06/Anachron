#!/usr/bin/env python3
"""Regenerate the README images from the KiCad project.

    python3 scripts/render_docs.py            # everything
    python3 scripts/render_docs.py 3d layers  # only some groups (3d, layers, sch)

Everything is written to docs/. The KiCad project itself is only ever read.

Requirements: KiCad 9+ (kicad-cli), Pillow, and macOS (SVGs are rasterised
with Quick Look's `qlmanage`, which ships with the OS).
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "Anachron"
PCB = PROJECT / "Anachron.kicad_pcb"
SCH = PROJECT / "Anachron.kicad_sch"
DOCS = ROOT / "docs"
IMAGES = DOCS / "images"

BG = "#0d1117"  # card background (edge of the gradient)
BG_GLOW = "#232c3b"  # card background (centre of the gradient)

# name -> (kicad-cli render args, output canvas size)
VIEWS_3D = {
    "hero": (["--side", "top", "--perspective", "--rotate", "-52,0,24"], (2000, 1180)),
    "board-top": (["--side", "top"], (1400, 1400)),
    "board-bottom": (["--side", "bottom"], (1400, 1400)),
    "board-angle-back": (["--side", "bottom", "--perspective", "--rotate", "-52,0,-24"], (1400, 1400)),
}

# name -> layers plotted (Edge.Cuts is always added)
VIEWS_LAYERS = {
    "layer-1-f-cu": ["F.Cu"],
    "layer-2-in1-cu": ["In1.Cu"],
    "layer-3-in2-cu": ["In2.Cu"],
    "layer-4-b-cu": ["B.Cu"],
    "assembly-front": ["F.Fab"],
}
ROUTING_LAYERS = ["B.Cu", "In2.Cu", "In1.Cu", "F.Cu"]


def find_kicad_cli():
    found = shutil.which("kicad-cli")
    if found:
        return found
    mac = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    if mac.exists():
        return str(mac)
    sys.exit("kicad-cli not found: install KiCad or add kicad-cli to PATH")


KICAD = find_kicad_cli()


def run(cmd):
    result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit("command failed: %s\n%s%s" % (" ".join(map(str, cmd)), result.stdout, result.stderr))


def rgb(hex_colour):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def save(img, name):
    IMAGES.mkdir(parents=True, exist_ok=True)
    path = IMAGES / (name + ".png")
    img.save(path, optimize=True)
    print("  %-28s %4dx%-4d %5d KB" % (path.name, img.width, img.height, path.stat().st_size // 1024))


def round_corners(img, radius):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def backdrop(size):
    """Dark card with a soft glow in the middle."""
    w, h = size
    glow = Image.radial_gradient("L").resize((int(w * 1.5), int(h * 1.5)), Image.BICUBIC)
    glow = ImageChops.invert(glow).crop(
        (int(w * 0.25), int(h * 0.25), int(w * 0.25) + w, int(h * 0.25) + h)
    )
    return Image.composite(Image.new("RGB", size, rgb(BG_GLOW)), Image.new("RGB", size, rgb(BG)), glow)


# --------------------------------------------------------------------------
# 3D renders
# --------------------------------------------------------------------------


def render_3d(tmp):
    print("3D renders")
    for name, (args, canvas) in VIEWS_3D.items():
        raw = tmp / (name + ".png")
        # Rendered large and scaled down afterwards for clean edges.
        run([KICAD, "pcb", "render", "-o", raw, "-w", 3600, "-h", 3600, "--quality", "basic",
             "--background", "transparent", "--zoom", "0.7"] + args + [PCB])
        board = Image.open(raw).convert("RGBA")
        board = board.crop(board.getbbox())

        w, h = canvas
        scale = min(w * 0.86 / board.width, h * 0.86 / board.height)
        board = board.resize((int(board.width * scale), int(board.height * scale)), Image.LANCZOS)
        x, y = (w - board.width) // 2, (h - board.height) // 2

        card = backdrop(canvas).convert("RGBA")
        shadow = Image.new("RGBA", canvas, (0, 0, 0, 0))
        shadow.paste((0, 0, 0, 150), (x, y + int(h * 0.018)), board.getchannel("A"))
        card.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(h * 0.02)))
        card.alpha_composite(board, (x, y))
        save(round_corners(card, 36), name)


# --------------------------------------------------------------------------
# SVG helpers (PCB layers and schematics)
# --------------------------------------------------------------------------

SVG_HEADER = re.compile(r'width="[^"]+"\s+height="[^"]+"\s+viewBox="([^"]+)"')


def svg_viewbox(text):
    return [float(v) for v in SVG_HEADER.search(text).group(1).split()]


def svg_reframe(text, x, y, w, h, background=None):
    """Point the SVG at a new viewBox, optionally painting a background behind it."""
    # The nominal size is deliberately huge: Quick Look scales big drawings down
    # to fill the thumbnail, but will not scale small ones all the way up.
    nominal = 1000.0 / max(w, h)
    header = 'width="%.4fmm" height="%.4fmm" viewBox="%.4f %.4f %.4f %.4f"' % (
        w * nominal, h * nominal, x, y, w, h)
    text = SVG_HEADER.sub(header, text, count=1)
    if background:
        rect = '<rect x="%.4f" y="%.4f" width="%.4f" height="%.4f" fill="%s"/>\n' % (x, y, w, h, background)
        first_group = text.index("<g ")
        text = text[:first_group] + rect + text[first_group:]
    return text


def rasterize(svg_text, tmp, size):
    """Rasterise a *square* SVG to a size x size image using Quick Look."""
    src = tmp / "raster.svg"
    src.write_text(svg_text)
    run(["qlmanage", "-t", "-s", size, "-o", tmp, src])
    return Image.open(tmp / "raster.svg.png").convert("RGB")


def content_bbox(img, inset=8, threshold=40):
    """Bounding box of everything that differs from the background colour.

    The outermost pixels are ignored: Quick Look blends them with the page edge.
    """
    inner = img.crop((inset, inset, img.width - inset, img.height - inset))
    background = Image.new("RGB", inner.size, inner.getpixel((0, 0)))
    diff = ImageChops.difference(inner, background).convert("L")
    box = diff.point(lambda p: 255 if p > threshold else 0).getbbox()
    if box is None:
        sys.exit("nothing was drawn in the rasterised SVG")
    return tuple(v + inset for v in box)


# --------------------------------------------------------------------------
# PCB layers
# --------------------------------------------------------------------------


def plot_layers(pcb, layers, out):
    # Page mode 1 keeps the SVG in page coordinates, so every plot lines up.
    run([KICAD, "pcb", "export", "svg", "--mode-single", "--page-size-mode", "1",
         "--exclude-drawing-sheet", "--layers", ",".join(layers + ["Edge.Cuts"]), "-o", out, pcb])
    return out.read_text()


def board_frame(tmp):
    """Square region (in SVG units) centred on the board outline, with a margin."""
    text = plot_layers(PCB, [], tmp / "edge.svg")
    _, _, w, h = svg_viewbox(text)
    side = max(w, h)
    text = re.sub(r"stroke:#[0-9A-Fa-f]{6}", "stroke:#FFFFFF", text)
    img = rasterize(svg_reframe(text, 0, 0, side, side, "#000000"), tmp, 3000)
    left, top, right, bottom = content_bbox(img)
    mm = side / img.width
    cx, cy = (left + right) / 2 * mm, (top + bottom) / 2 * mm
    frame = max(right - left, bottom - top) * mm * 1.1
    return cx - frame / 2, cy - frame / 2, frame


def strip_zones(pcb_text):
    """Remove every copper zone so only tracks, pads and vias are plotted."""
    out, pos = [], 0
    for match in re.finditer(r"\n\t\(zone\b", pcb_text):
        start = match.start()
        if start < pos:
            continue
        depth, i, in_string = 0, start + 1, False
        while True:
            ch = pcb_text[i]
            if in_string:
                if ch == "\\":
                    i += 1
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(pcb_text[pos:start])
        pos = i + 1
    out.append(pcb_text[pos:])
    return "".join(out)


def layer_card(svg_text, frame, tmp):
    x, y, side = frame
    # KiCad draws drill holes in white/black: make them see-through instead.
    svg_text = svg_text.replace("#FFFFFF", BG).replace("#000000", BG)
    img = rasterize(svg_reframe(svg_text, x, y, side, side, BG), tmp, 2800)
    return round_corners(img.resize((1400, 1400), Image.LANCZOS), 36)


def render_layers(tmp):
    print("PCB layers")
    frame = board_frame(tmp)
    for name, layers in VIEWS_LAYERS.items():
        save(layer_card(plot_layers(PCB, layers, tmp / (name + ".svg")), frame, tmp), name)

    bare = tmp / "bare"
    bare.mkdir()
    (bare / PCB.name).write_text(strip_zones(PCB.read_text()))
    save(layer_card(plot_layers(bare / PCB.name, ROUTING_LAYERS, tmp / "routing.svg"), frame, tmp), "routing-all-layers")


# --------------------------------------------------------------------------
# Schematics
# --------------------------------------------------------------------------


def render_schematics(tmp):
    print("Schematics")
    DOCS.mkdir(parents=True, exist_ok=True)
    run([KICAD, "sch", "export", "pdf", "-o", DOCS / "Anachron-schematic.pdf", SCH])
    print("  Anachron-schematic.pdf")

    out = tmp / "sch"
    run([KICAD, "sch", "export", "svg", "--exclude-drawing-sheet", "-o", out, SCH])
    for svg in sorted(out.glob("*.svg")):
        sheet = svg.stem[len(SCH.stem):].strip("-") or "overview"
        name = "sch-" + re.sub(r"[^a-z0-9]+", "-", sheet.lower()).strip("-")

        text = svg.read_text()
        x, y, w, h = svg_viewbox(text)
        side = max(w, h)
        img = rasterize(svg_reframe(text, x, y, side, side), tmp, 4000)
        img = img.crop((0, 0, round(img.width * w / side), round(img.height * h / side)))

        # Trim the empty page around the drawing, leaving a small margin.
        box = content_bbox(img)
        pad = 50
        img = img.crop((max(box[0] - pad, 0), max(box[1] - pad, 0),
                        min(box[2] + pad, img.width), min(box[3] + pad, img.height)))
        if img.width > 2400:
            img = img.resize((2400, round(img.height * 2400 / img.width)), Image.LANCZOS)
        save(round_corners(img, 16), name)


GROUPS = {"3d": render_3d, "layers": render_layers, "sch": render_schematics}


def main():
    wanted = sys.argv[1:] or list(GROUPS)
    unknown = [g for g in wanted if g not in GROUPS]
    if unknown:
        sys.exit("unknown group(s): %s (choose from %s)" % (", ".join(unknown), ", ".join(GROUPS)))
    with tempfile.TemporaryDirectory() as tmp:
        for group in wanted:
            GROUPS[group](Path(tmp))


if __name__ == "__main__":
    main()
