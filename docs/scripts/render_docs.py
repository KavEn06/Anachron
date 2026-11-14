#!/usr/bin/env python3
"""Regenerate the README images.

    python3 docs/scripts/render_docs.py            # everything
    python3 docs/scripts/render_docs.py 3d layers  # only some groups (3d, layers, sch, fusion)

The 3d, layers and sch groups are generated from the KiCad project, which is
only ever read. The fusion group takes the Fusion 360 renders saved in
docs/fusion/ and puts them on the same dark cards; it also builds the gif of
mockup watch faces on the assembled render from the pictures in docs/faces/.
Everything is written to docs/.

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

ROOT = Path(__file__).resolve().parents[2]  # docs/scripts/ -> repository root
PROJECT = ROOT / "kicad"
PCB = PROJECT / "Anachron.kicad_pcb"
SCH = PROJECT / "Anachron.kicad_sch"
DOCS = ROOT / "docs"
IMAGES = DOCS / "images"

BG = "#0d1117"  # card background (edge of the gradient)
BG_GLOW = "#232c3b"  # card background (centre of the gradient)

# name -> (kicad-cli render args, output canvas size)
# name -> (kicad-cli render args, card width). The card height follows the
# board, so every card has the same narrow margin all the way round.
VIEWS_3D = {
    "hero": (["--side", "top", "--perspective", "--rotate", "-52,0,24"], 2000),
    "board-top": (["--side", "top"], 1400),
    "board-bottom": (["--side", "bottom"], 1400),
    "board-angle-back": (["--side", "bottom", "--perspective", "--rotate", "-52,0,-24"], 2000),
}
MARGIN_3D = 0.04  # margin around the board, as a fraction of the card width

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
    for name, (args, w) in VIEWS_3D.items():
        raw = tmp / (name + ".png")
        # Rendered large and scaled down afterwards for clean edges.
        run([KICAD, "pcb", "render", "-o", raw, "-w", 3600, "-h", 3600, "--quality", "basic",
             "--background", "transparent", "--zoom", "0.7"] + args + [PCB])
        board = Image.open(raw).convert("RGBA")
        board = board.crop(board.getbbox())

        margin = round(w * MARGIN_3D)
        scale = (w - 2 * margin) / board.width
        board = board.resize((w - 2 * margin, round(board.height * scale)), Image.LANCZOS)
        h = board.height + 2 * margin
        canvas = (w, h)
        x = y = margin

        card = backdrop(canvas).convert("RGBA")
        shadow = Image.new("RGBA", canvas, (0, 0, 0, 0))
        shadow.paste((0, 0, 0, 150), (x, y + round(margin * 0.25)), board.getchannel("A"))
        card.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(margin * 0.3)))
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
FACES = DOCS / "faces"  # pictures used on the mockup watch faces
FACE_IMAGE = "anachron-assembled"  # the render whose blank display gets a watch face
BRASS = (201, 164, 92)
ACCENT = (86, 156, 240)
RECORD_ANGLE = 35  # how far the record is turned on the now-playing face, in degrees


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


def _vinyl_disc(diameter):
    """The record cut out of docs/faces/vinyl.png as a round RGBA image."""
    src = Image.open(FACES / "vinyl.png").convert("RGB")
    a = np.asarray(src).astype(int)
    backdrop_colour = np.median(np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]]), 0)
    # Far from the backdrop colour = the record itself (its soft shadow is closer).
    solid = ndi.binary_fill_holes(np.abs(a - backdrop_colour).sum(2) > 200)
    labels, _ = ndi.label(solid)
    ys, xs = np.nonzero(labels == np.argmax(np.bincount(labels.ravel())[1:]) + 1)
    cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    r = min(xs.max() - xs.min(), ys.max() - ys.min()) / 2 - 3  # stay inside the edge
    disc = src.crop((round(cx - r), round(cy - r), round(cx + r), round(cy + r)))
    disc = disc.resize((diameter, diameter), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (diameter * 2, diameter * 2), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, diameter * 2 - 1, diameter * 2 - 1], fill=255)
    disc.putalpha(mask.resize((diameter, diameter), Image.LANCZOS))
    return disc


_DISC = {}


def draw_spotify_face(size, angle=0):
    """A now-playing face: the record from docs/faces/vinyl.png, turned by `angle`."""
    s = size * 2
    face = Image.new("RGB", (s, s), (8, 10, 14))
    draw = ImageDraw.Draw(face)
    c = s / 2

    diameter = round(s * 0.76)
    if diameter not in _DISC:
        _DISC[diameter] = _vinyl_disc(diameter)
    disc = _DISC[diameter].rotate(-angle, resample=Image.BICUBIC)
    face.paste(disc, (round(c - diameter / 2), round(c - diameter / 2)), disc)

    green = (30, 215, 96)
    draw.text((c, s * 0.068), "NOW PLAYING", font=_face_font(round(s * 0.03)), fill=green, anchor="mm")
    # Progress bar under the record.
    y, x0, x1, w = s * 0.932, s * 0.39, s * 0.61, round(s * 0.008)
    draw.line([(x0, y), (x1, y)], fill=(60, 66, 76), width=w)
    draw.line([(x0, y), (x0 + (x1 - x0) * 0.4, y)], fill=green, width=w)
    return face.resize((size, size), Image.LANCZOS)


def _pet_sprite(width):
    """The smiley from docs/faces/pet.png, trimmed to its drawing."""
    a = np.asarray(Image.open(FACES / "pet.png").convert("RGBA")).copy()
    # Keep only what is inside the black outline; the pale fringe around it is
    # left over from the white page the smiley was drawn on.
    outline = (a[..., :3].max(2) < 120) & (a[..., 3] > 128)
    inside = ndi.binary_fill_holes(ndi.binary_closing(outline, iterations=2))
    a[~inside, :3] = 0
    a[..., 3] = np.uint8(np.clip(ndi.gaussian_filter(inside.astype(float), 0.8), 0, 1) * 255)
    ys, xs = np.nonzero(inside)
    src = Image.fromarray(a).crop((xs.min() - 2, ys.min() - 2, xs.max() + 3, ys.max() + 3))
    return src.resize((width, round(src.height * width / src.width)), Image.LANCZOS)


def draw_pet_face(size):
    """A Tamagotchi-style face: just the smiley."""
    s = size * 2
    face = Image.new("RGB", (s, s), (14, 18, 28))
    c = s / 2
    sprite = _pet_sprite(round(s * 0.5))
    face.paste(sprite, (round(c - sprite.width / 2), round(c - sprite.height / 2)), sprite)
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
    """Static clock PNG plus a GIF that fades between the clock, record and pet faces."""
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
    record_f = frame_from(draw_spotify_face(size, RECORD_ANGLE))

    frames, durations = [], []

    def hold(frame, ms):
        frames.append(frame)
        durations.append(ms)

    def crossfade(a, b, steps=5, ms=80):
        for i in range(1, steps + 1):
            frames.append(_blend(a, b, i / (steps + 1)))
            durations.append(ms)

    # Each face is a still; the gif only fades from one to the next.
    for shown, following in [(clock_f, record_f), (record_f, pet_f), (pet_f, clock_f)]:
        hold(shown, 2200)
        crossfade(shown, following)

    # One palette for every frame, picked from all three faces. A fixed grain
    # (the same in every frame) hides banding in the dark backdrop without
    # making the still parts of the picture differ between frames, so only the
    # screen has to be stored for most of them.
    sheet = Image.new("RGB", (width * 2, height * 2))
    for i, sample in enumerate([clock_f, record_f, record_f, pet_f]):
        sheet.paste(sample, (width * (i % 2), height * (i // 2)))
    # The small accents (green text, seconds hand) cover too few pixels to win
    # palette entries on their own, so they are added by hand, with darker
    # steps for their soft edges.
    accents = [tuple(round(v * k) for v in colour)
               for colour in [(30, 215, 96), ACCENT] for k in (1, 0.75, 0.5, 0.3)]
    base = sheet.quantize(colors=256 - len(accents), method=Image.MEDIANCUT, dither=Image.NONE)
    palette = Image.new("P", (1, 1))
    palette.putpalette(base.getpalette()[:(256 - len(accents)) * 3] + [v for c in accents for v in c])
    grain = np.random.default_rng(0).uniform(-2.5, 2.5, (height, width, 1))

    def to_palette(frame):
        noisy = np.uint8(np.clip(np.asarray(frame).astype(float) + grain, 0, 255))
        return Image.fromarray(noisy).quantize(palette=palette, dither=Image.NONE)

    frames = [to_palette(f) for f in frames]
    path = IMAGES / (FACE_IMAGE + ".gif")
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations,
                   loop=0, optimize=False)
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
