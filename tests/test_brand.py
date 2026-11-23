"""The icons are what Windows and Impossible OS can use, and helio.exe shows them."""
import hashlib
import json
import os
import re
import struct
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

BRAND = Path(__file__).resolve().parents[1] / "brand"
RT_ICON, RT_GROUP_ICON = 3, 14
ATTRIBUTES = {"viewBox", "width", "height", "id", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "d", "offset", "gradientUnits",
              "fill", "fill-opacity", "stroke", "stroke-width", "stroke-opacity", "stroke-linecap", "stroke-linejoin", "stop-color", "stop-opacity"}


def images():
    """(size, bytes) of each picture in icon.ico."""
    data = (BRAND / "icon.ico").read_bytes()
    for i in range(struct.unpack_from("<H", data, 4)[0]):
        length, at = struct.unpack_from("<II", data, 6 + 16 * i + 8)
        yield data[6 + 16 * i] or 256, data[at:at + length]


def resources(exe):
    """{(type, name): bytes} of a 64-bit program's resources, found as Windows finds them."""
    pe = struct.unpack_from("<I", exe, 0x3C)[0]
    if exe[:2] != b"MZ" or exe[pe:pe + 4] != b"PE\0\0" or struct.unpack_from("<H", exe, pe + 24)[0] != 0x20B:
        raise ValueError("not a 64-bit Windows program")
    count, optional = struct.unpack_from("<H", exe, pe + 6)[0], struct.unpack_from("<H", exe, pe + 20)[0]
    root = struct.unpack_from("<I", exe, pe + 24 + 112 + 8 * 2)[0]
    sections = [struct.unpack_from("<IIII", exe, pe + 24 + optional + 40 * i + 8) for i in range(count)]

    def at(address):
        for size, start, raw_size, raw in sections:
            if start <= address < start + max(size, raw_size):
                return raw + address - start
        raise ValueError("an address outside every section")

    found = {}

    def walk(offset, path):
        base = at(root)
        named, numbered = struct.unpack_from("<HH", exe, base + offset + 12)
        for i in range(named + numbered):
            name, target = struct.unpack_from("<II", exe, base + offset + 16 + 8 * i)
            if target & 0x80000000:
                walk(target & 0x7FFFFFFF, path + (name,))
            else:
                address, size = struct.unpack_from("<II", exe, base + target)
                found[path[:2]] = exe[at(address):at(address) + size]

    if root:
        walk(0, ())
    return found


class Brand(unittest.TestCase):
    def test_the_icons_use_only_what_the_os_renders(self):
        """The system SVG profile of Impossible OS: no mask, filter, text, CSS or script, and every gradient is there."""
        allowed = {"svg", "defs", "linearGradient", "radialGradient", "stop", "path", "rect", "circle"}
        icons = sorted((BRAND / "icon").glob("*.svg"))
        self.assertEqual([p.name for p in icons], ["glyph.svg", "icon-16.svg", "icon-20.svg", "icon-24.svg", "icon-32.svg", "icon.svg"])
        for path in icons:
            elements = list(ET.parse(path).iter())
            self.assertEqual({e.tag.split("}")[1] for e in elements} - allowed, set(), path.name)
            self.assertEqual({name for e in elements for name in e.attrib} - ATTRIBUTES, set(), path.name)
            paints = [v for e in elements for n, v in e.attrib.items() if n in ("fill", "stroke", "stop-color")]
            self.assertEqual([v for v in paints if not re.fullmatch(r"none|currentColor|#[0-9A-Fa-f]{6}|url\(#[\w-]+\)", v)], [], path.name)
            used = {v[5:-1] for e in elements for v in e.attrib.values() if v.startswith("url(#")}
            self.assertEqual(used, {e.get("id") for e in elements if e.get("id")}, path.name)

    def test_each_picture_is_the_one_rendered_from_its_drawing(self):
        def digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()
        record = json.loads((BRAND / "png/rendered.json").read_text(encoding="utf-8"))
        self.assertEqual(set(record), {p.name for p in (BRAND / "png").glob("*.png")})
        self.assertEqual({r["drawing"] for r in record.values()}, {p.name for p in (BRAND / "icon").glob("icon*.svg")})
        for name, r in record.items():
            self.assertEqual((r["drawing_sha256"], r["sha256"]), (digest(BRAND / "icon" / r["drawing"]), digest(BRAND / "png" / name)), f"{name}: run tools/brand.py")

    def test_the_icon_file_has_every_size(self):
        self.assertEqual([size for size, _ in images()], [16, 20, 24, 32, 48, 64, 256])
        for size, picture in images():
            self.assertEqual(picture, (BRAND / f"png/icon-{size}.png").read_bytes(), size)

    def test_windows_finds_the_icon_in_the_program(self):
        found = resources((Path(os.environ["IMPOSSIBLE_BUILD"]) / "helio.exe").read_bytes())
        group = found[RT_GROUP_ICON, 1]
        shown = {}
        for i in range(struct.unpack_from("<H", group, 4)[0]):
            width, number = group[6 + 14 * i] or 256, struct.unpack_from("<H", group, 6 + 14 * i + 12)[0]
            shown[width] = found[RT_ICON, number]
        self.assertEqual(shown, dict(images()))


if __name__ == "__main__":
    unittest.main()
