"""The icons are what Windows and Impossible OS can use, and helio.exe has them."""
import os
import re
import struct
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

BRAND = Path(__file__).resolve().parents[1] / "brand"


def images():
    """(size, bytes) of each picture in icon.ico."""
    data = (BRAND / "icon.ico").read_bytes()
    for i in range(struct.unpack_from("<H", data, 4)[0]):
        length, at = struct.unpack_from("<II", data, 6 + 16 * i + 8)
        yield data[6 + 16 * i] or 256, data[at:at + length]


class Brand(unittest.TestCase):
    def test_the_icons_use_only_what_the_os_renders(self):
        """The system SVG profile of Impossible OS: no mask, filter, text or CSS, and every gradient is there."""
        allowed = {"svg", "defs", "linearGradient", "radialGradient", "stop", "path", "rect", "circle"}
        icons = sorted((BRAND / "icon").glob("*.svg"))
        self.assertEqual([p.name for p in icons], ["glyph.svg", "icon-16.svg", "icon-20.svg", "icon-24.svg", "icon-32.svg", "icon.svg"])
        for path in icons:
            text = path.read_text(encoding="utf-8")
            self.assertEqual({e.tag.split("}")[1] for e in ET.fromstring(text).iter()} - allowed, set(), path.name)
            self.assertEqual(set(re.findall(r"url\(#([^)]+)\)", text)), set(re.findall(r'id="([^"]+)"', text)), path.name)
            self.assertNotIn("style=", text, path.name)

    def test_the_icon_file_has_every_size(self):
        self.assertEqual(sorted(size for size, _ in images()), [16, 20, 24, 32, 48, 64, 256])

    def test_the_windows_program_has_the_icon(self):
        program = (Path(os.environ["IMPOSSIBLE_BUILD"]) / "helio.exe").read_bytes()
        for size, picture in images():
            self.assertIn(picture, program, size)


if __name__ == "__main__":
    unittest.main()
