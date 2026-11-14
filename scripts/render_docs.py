#!/usr/bin/env python3
"""Regenerate the README images.

    python3 scripts/render_docs.py            # everything
    python3 scripts/render_docs.py 3d layers  # only some groups (3d, layers, sch, fusion)

The 3d, layers and sch groups are generated from the KiCad project, which is
only ever read. The fusion group takes the Fusion 360 renders saved in
docs/fusion/ and puts them on the same dark cards. Everything is written to
docs/.

Requirements: KiCad 9+ (kicad-cli), Pillow, NumPy, SciPy, and macOS (SVGs are
rasterised with Quick Look's `qlmanage`, and the watch face uses a system font).
"""

import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
from scipy import ndimage as ndi

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
    # Page 1 is the top-level sheet, which only holds the three sheet symbols.
    run([KICAD, "sch", "export", "pdf", "--pages", "2,3,4", "-o", DOCS / "Anachron-schematic.pdf", SCH])
    print("  Anachron-schematic.pdf")

    out = tmp / "sch"
    run([KICAD, "sch", "export", "svg", "--exclude-drawing-sheet", "-o", out, SCH])
    for svg in sorted(out.glob("*.svg")):
        sheet = svg.stem[len(SCH.stem):].strip("-")
        if not sheet:
            continue
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


# --------------------------------------------------------------------------
# Fusion 360 renders
# --------------------------------------------------------------------------

FUSION = DOCS / "fusion"
FACE_IMAGE = "anachron-assembled"  # the render whose blank display gets a watch face
BRASS = (201, 164, 92)
ACCENT = (86, 156, 240)


def fusion_background(a):
    """Mask of Fusion's light grey backdrop, including the soft floor shadows."""
    a = a.astype(np.int16)
    chroma = a.max(2) - a.min(2)
    luma = a.mean(2)
    edge = np.concatenate([a[:8].reshape(-1, 3), a[-8:].reshape(-1, 3)])
    ref = float(np.median(edge, 0).mean())
    neutral = chroma <= 6

    # Plain backdrop reachable from the image border, plus enclosed pockets of
    # it (seen through rings and holes). The PCB is too dark to be mistaken
    # for it, which is why the luma floor is so high.
    core = neutral & (luma >= 190) & (luma <= ref + 4)
    labels, count = ndi.label(core)
    index = np.arange(count + 1)
    keep = np.zeros(count + 1, bool)
    border = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    keep[np.unique(border)] = True
    sizes = ndi.sum(np.ones_like(luma), labels, index)
    means = ndi.mean(luma, labels, index)
    spread = ndi.standard_deviation(luma, labels, index)
    keep |= (sizes > 100) & (np.abs(means - ref) < 6) & (spread < 10)
    keep[0] = False
    background = keep[labels]

    # Grow into shadows: nearly neutral (they pick up a slight tint from the
    # case), but darker. Shadows are smooth, so a gradient limit stops the
    # growth at the sharp outline of neutral parts.
    smooth = ndi.gaussian_filter(luma, 1.0)
    gradient = np.hypot(ndi.sobel(smooth, 0), ndi.sobel(smooth, 1)) / 8
    allowed = (chroma <= 10) & (luma >= 30) & (gradient < 4)
    labels, count = ndi.label(allowed | background)
    keep = np.zeros(count + 1, bool)
    keep[np.unique(labels[background])] = True
    keep[0] = False
    background = keep[labels]

    # Specks and islands of shadow cut off by the gradient limit.
    labels, count = ndi.label(~background)
    index = np.arange(count + 1)
    sizes = ndi.sum(np.ones_like(luma), labels, index)
    grey = ndi.mean(chroma <= 10, labels, index)
    stray = (sizes < 400) | ((sizes < luma.size * 0.003) & (grey > 0.95))
    background |= stray[labels] & (labels > 0)

    # Grey fringe along outlines that matches the shadow right next to it.
    for _ in range(6):
        weight = ndi.uniform_filter(background.astype(float), 3)
        nearby = ndi.uniform_filter(np.where(background, luma, 0), 3) / np.maximum(weight, 1e-6)
        fringe = (weight > 0) & ~background & (chroma <= 12) & (np.abs(luma - nearby) < 12)
        if not fringe.any():
            break
        background |= fringe
    return background, luma / ref


def _face_font(size, index=0):
    return ImageFont.truetype("/System/Library/Fonts/Avenir Next.ttc", size, index=index)


def draw_clock_face(size):
    """A flat, round analog dial showing 10:09."""
    s = size * 2  # drawn at double size, then scaled down for smooth edges
    face = Image.new("RGB", (s, s), (6, 8, 12))
    draw = ImageDraw.Draw(face)
    c = s / 2

    def polar(radius, degrees):
        t = math.radians(degrees - 90)
        return c + radius * math.cos(t), c + radius * math.sin(t)

    def hand(length, tail, width, degrees, colour):
        draw.line([polar(-tail, degrees), polar(length, degrees)], fill=colour, width=width)

    for minute in range(60):
        degrees = minute * 6
        if minute % 5:
            draw.line([polar(s * 0.455, degrees), polar(s * 0.475, degrees)], fill=(120, 126, 138), width=round(s * 0.004))
        else:
            width = round(s * (0.02 if minute == 0 else 0.012))
            draw.line([polar(s * 0.39, degrees), polar(s * 0.475, degrees)], fill=BRASS, width=width)

    title = _face_font(round(s * 0.042))
    small = _face_font(round(s * 0.034))
    draw.text((c, s * 0.27), "A N A C H R O N", font=title, fill=(214, 216, 222), anchor="mm")
    draw.rounded_rectangle([s * 0.66, c - s * 0.032, s * 0.8, c + s * 0.032], radius=s * 0.008,
                           outline=(90, 96, 108), width=round(s * 0.003))
    draw.text((s * 0.73, c), "SAT 14", font=small, fill=(214, 216, 222), anchor="mm")

    hand(s * 0.24, s * 0.04, round(s * 0.022), (10 + 9 / 60) * 30, BRASS)
    hand(s * 0.36, s * 0.05, round(s * 0.014), 9 * 6, BRASS)
    hand(s * 0.41, s * 0.09, round(s * 0.005), 36 * 6, ACCENT)
    draw.ellipse([c - s * 0.022, c - s * 0.022, c + s * 0.022, c + s * 0.022], fill=BRASS)
    draw.ellipse([c - s * 0.008, c - s * 0.008, c + s * 0.008, c + s * 0.008], fill=ACCENT)
    return face.resize((size, size), Image.LANCZOS)


def draw_spotify_face(size, angle=0):
    """A CD / vinyl face with a rotating album disc."""
    s = size * 2
    face = Image.new("RGB", (s, s), (8, 10, 14))
    draw = ImageDraw.Draw(face)
    c = s / 2

    # Soft ring that stays fixed while the disc spins.
    draw.ellipse([s * 0.05, s * 0.05, s * 0.95, s * 0.95], outline=(28, 34, 42), width=round(s * 0.012))

    disc = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(disc)
    # Vinyl plate with concentric grooves.
    d.ellipse([s * 0.08, s * 0.08, s * 0.92, s * 0.92], fill=(18, 18, 22))
    for r in range(int(s * 0.18), int(s * 0.44), max(round(s * 0.012), 1)):
        d.ellipse([c - r, c - r, c + r, c + r], outline=(32, 32, 38), width=max(round(s * 0.002), 1))
    # Label / album art in the middle.
    label_r = s * 0.17
    d.ellipse([c - label_r, c - label_r, c + label_r, c + label_r], fill=(30, 215, 96))
    # Abstract album art: warm bars across a dark square inside the green label.
    art = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    a = ImageDraw.Draw(art)
    pad = s * 0.055
    a.rounded_rectangle([c - label_r + pad, c - label_r + pad, c + label_r - pad, c + label_r - pad],
                        radius=s * 0.02, fill=(18, 22, 28))
    for i, colour in enumerate([(232, 90, 68), (240, 180, 70), (90, 170, 230), (180, 110, 220)]):
        y0 = c - label_r + pad + s * 0.02 + i * s * 0.045
        a.rectangle([c - label_r + pad + s * 0.02, y0, c + label_r - pad - s * 0.02, y0 + s * 0.032], fill=colour)
    disc = Image.alpha_composite(disc, art)
    # Spindle hole and a highlight wedge so rotation is visible.
    d = ImageDraw.Draw(disc)
    d.pieslice([s * 0.08, s * 0.08, s * 0.92, s * 0.92], -18, 8, fill=(255, 255, 255, 28))
    d.ellipse([c - s * 0.028, c - s * 0.028, c + s * 0.028, c + s * 0.028], fill=(8, 10, 14))
    disc = disc.rotate(-angle, resample=Image.BICUBIC, center=(c, c))
    face.paste(disc, (0, 0), disc)

    title = _face_font(round(s * 0.038))
    small = _face_font(round(s * 0.03))
    draw.text((c, s * 0.12), "NOW PLAYING", font=small, fill=(30, 215, 96), anchor="mm")
    draw.text((c, s * 0.88), "Midnight Tram", font=title, fill=(230, 232, 236), anchor="mm")
    return face.resize((size, size), Image.LANCZOS)


def draw_pet_face(size):
    """A tiny Tamagotchi-style creature on a simple face."""
    s = size * 2
    face = Image.new("RGB", (s, s), (14, 18, 28))
    draw = ImageDraw.Draw(face)
    c = s / 2

    # Soft vignette ring.
    draw.ellipse([s * 0.06, s * 0.06, s * 0.94, s * 0.94], outline=(40, 52, 72), width=round(s * 0.01))

    # Creature body.
    body = [c - s * 0.18, c - s * 0.08, c + s * 0.18, c + s * 0.28]
    draw.ellipse(body, fill=(255, 196, 110))
    # Ears.
    draw.ellipse([c - s * 0.22, c - s * 0.22, c - s * 0.05, c - s * 0.02], fill=(255, 196, 110))
    draw.ellipse([c + s * 0.05, c - s * 0.22, c + s * 0.22, c - s * 0.02], fill=(255, 196, 110))
    draw.ellipse([c - s * 0.175, c - s * 0.175, c - s * 0.09, c - s * 0.07], fill=(255, 150, 150))
    draw.ellipse([c + s * 0.09, c - s * 0.175, c + s * 0.175, c - s * 0.07], fill=(255, 150, 150))
    # Eyes and blush.
    draw.ellipse([c - s * 0.1, c + s * 0.02, c - s * 0.04, c + s * 0.1], fill=(28, 24, 36))
    draw.ellipse([c + s * 0.04, c + s * 0.02, c + s * 0.1, c + s * 0.1], fill=(28, 24, 36))
    draw.ellipse([c - s * 0.085, c + s * 0.035, c - s * 0.06, c + s * 0.06], fill=(255, 255, 255))
    draw.ellipse([c + s * 0.055, c + s * 0.035, c + s * 0.08, c + s * 0.06], fill=(255, 255, 255))
    draw.ellipse([c - s * 0.16, c + s * 0.12, c - s * 0.1, c + s * 0.16], fill=(255, 140, 140, ))
    draw.ellipse([c + s * 0.1, c + s * 0.12, c + s * 0.16, c + s * 0.16], fill=(255, 140, 140))
    # Smile.
    draw.arc([c - s * 0.06, c + s * 0.12, c + s * 0.06, c + s * 0.2], 20, 160, fill=(28, 24, 36), width=round(s * 0.012))

    title = _face_font(round(s * 0.036))
    small = _face_font(round(s * 0.028))
    draw.text((c, s * 0.14), "PET", font=small, fill=ACCENT, anchor="mm")
    draw.text((c, s * 0.86), "hungry · 3h", font=title, fill=(210, 214, 222), anchor="mm")
    return face.resize((size, size), Image.LANCZOS)


def screen_map(img):
    """Affine that maps a square dial onto the dark elliptical display."""
    a = np.asarray(img).astype(np.int16)
    dark = (a.max(2) < 40) & (a.max(2) - a.min(2) < 12)
    labels, count = ndi.label(dark)
    screen = ndi.binary_fill_holes(labels == np.argmax(np.bincount(labels.ravel())[1:]) + 1)

    ys, xs = np.nonzero(screen)
    centre = np.array([xs.mean(), ys.mean()])
    cov = np.cov(np.stack([xs - centre[0], ys - centre[1]]))
    values, vectors = np.linalg.eigh(cov)
    # A filled ellipse with semi-axes r has variance r^2 / 4 along each axis.
    to_screen = vectors @ np.diag(2 * np.sqrt(values)) @ vectors.T

    # Turn the dial so that 12 o'clock points at the crown: the brightest
    # brass-coloured pixels above the screen.
    brass = (a[..., 0] > 150) & (a[..., 0] - a[..., 2] > 50) & (np.arange(a.shape[0])[:, None] < ys.min())
    by, bx = np.nonzero(brass)
    crown = np.array([bx[by < by.min() + 20].mean(), by.min()])
    want = (crown - centre) / np.linalg.norm(crown - centre)
    best = min(range(-90, 91), key=lambda d: -np.dot(want, _unit(to_screen @ _rot(d) @ [0, -1])))
    to_screen = to_screen @ _rot(best)

    size = 1600
    active = 0.9  # the panel's black border inside the glass
    to_face = np.linalg.inv(to_screen) * (size / 2 / active)
    offset = np.array([size / 2, size / 2]) - to_face @ centre
    coeffs = (to_face[0, 0], to_face[0, 1], offset[0], to_face[1, 0], to_face[1, 1], offset[1])

    disc = Image.new("L", (size, size), 0)
    ImageDraw.Draw(disc).ellipse([size * (1 - active) / 2] * 2 + [size * (1 + active) / 2] * 2, fill=255)
    disc = disc.filter(ImageFilter.GaussianBlur(3))
    alpha = disc.transform(img.size, Image.AFFINE, coeffs, Image.BICUBIC)
    return size, coeffs, alpha


def paint_face(img, face, coeffs, alpha):
    """Screen-blend a dial onto the glass so reflections stay on top."""
    warped = face.transform(img.size, Image.AFFINE, coeffs, Image.BICUBIC)
    base = np.asarray(img).astype(float) / 255
    dial = np.asarray(warped).astype(float) / 255
    lit = 1 - (1 - base) * (1 - dial)
    k = (np.asarray(alpha).astype(float) / 255)[..., None]
    return Image.fromarray(np.uint8(np.round((base * (1 - k) + lit * k) * 255)))


def _rot(degrees):
    t = math.radians(degrees)
    return np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])


def _unit(v):
    return np.asarray(v) / np.linalg.norm(v)


def fusion_mat(img):
    """Background mask and contact-shadow shading for a Fusion render."""
    a = np.asarray(img)
    background, brightness = fusion_background(a)
    obj = ndi.binary_erosion(~background, iterations=1)
    alpha = ndi.gaussian_filter(obj.astype(float), 0.8)[..., None]
    solid = ndi.binary_erosion(obj, iterations=2)
    _, indices = ndi.distance_transform_edt(~solid, return_indices=True)
    shade = 1 - np.clip(1 - ndi.gaussian_filter(brightness, 2), 0, 1) * 1.6
    card = np.asarray(backdrop(img.size)).astype(float) * np.clip(shade, 0, 1)[..., None]
    return alpha, indices, card


def style_fusion(img, mat=None):
    """Put a Fusion render on the same dark card as the KiCad images."""
    a = np.asarray(img)
    alpha, indices, card = mat if mat is not None else fusion_mat(img)
    iy, ix = indices
    colour = a[iy, ix].astype(float)
    out = colour * alpha + card * (1 - alpha)
    out = Image.fromarray(np.uint8(np.clip(np.round(out), 0, 255)))
    return round_corners(out, round(min(img.size) * 0.026))


def _blend(a, b, t):
    return Image.fromarray(np.uint8(np.round(np.asarray(a).astype(float) * (1 - t) + np.asarray(b).astype(float) * t)))


def render_hero_faces(blank, coeffs, alpha):
    """Static clock PNG plus a GIF that cycles clock → Spotify → pet."""
    size = 1600
    width = 900
    height = round(blank.height * width / blank.width)
    # Faces only change the screen, so the backdrop mat from the blank case is fine.
    mat = fusion_mat(blank)

    def frame_from(face_img):
        card = style_fusion(paint_face(blank, face_img, coeffs, alpha), mat).convert("RGBA")
        bg = Image.new("RGBA", card.size, rgb(BG) + (255,))
        return Image.alpha_composite(bg, card).convert("RGB").resize((width, height), Image.LANCZOS)

    clock_f = frame_from(draw_clock_face(size))
    # Full-resolution static PNG keeps the same look as the other cards.
    save(style_fusion(paint_face(blank, draw_clock_face(size), coeffs, alpha), mat), FACE_IMAGE)

    pet_f = frame_from(draw_pet_face(size))
    spin = [frame_from(draw_spotify_face(size, angle)) for angle in range(0, 360, 30)]

    frames, durations = [], []

    def hold(frame, ms):
        frames.append(frame)
        durations.append(ms)

    def crossfade(a, b, steps=5, ms=80):
        for i in range(1, steps + 1):
            frames.append(_blend(a, b, i / (steps + 1)))
            durations.append(ms)

    hold(clock_f, 1600)
    crossfade(clock_f, spin[0])
    for f in spin + spin:  # two turns of the disc
        hold(f, 90)
    crossfade(spin[-1], pet_f)
    hold(pet_f, 1600)
    crossfade(pet_f, clock_f)

    path = IMAGES / (FACE_IMAGE + ".gif")
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations,
                   loop=0, optimize=True)
    print("  %-28s %4dx%-4d %5d KB" % (path.name, width, height, path.stat().st_size // 1024))


def render_fusion(tmp):
    print("Fusion renders")
    sources = sorted(FUSION.glob("*.png"))
    if not sources:
        sys.exit("no renders found in %s" % FUSION.relative_to(ROOT))

    blank = Image.open(FUSION / (FACE_IMAGE + ".png")).convert("RGB")
    _, coeffs, alpha = screen_map(blank)
    render_hero_faces(blank, coeffs, alpha)

    for src in sources:
        if src.stem == FACE_IMAGE:
            continue
        save(style_fusion(Image.open(src).convert("RGB")), src.stem)


GROUPS = {"3d": render_3d, "layers": render_layers, "sch": render_schematics, "fusion": render_fusion}


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
