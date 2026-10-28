"""The documented logo palette must match the supplied vector artwork."""
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


LOGOS = Path(__file__).resolve().parents[1] / "brand" / "logo" / "yobiru"


class LogoPaletteTests(unittest.TestCase):
    def test_documented_palette_matches_color_and_orange_svg_sources(self):
        readme = (LOGOS / "README.md").read_text(encoding="utf-8")
        documented = {color.upper() for color in re.findall(r"#[0-9a-fA-F]{6}\b", readme)}
        actual = set()
        for variant in ("color", "orange"):
            for component in ("symbol", "wordmark", "lockup"):
                source = LOGOS / ("yobiru-%s-%s.svg" % (component, variant))
                actual.update(node.attrib["fill"].upper() for node in ET.parse(source).iter()
                              if "fill" in node.attrib)
        self.assertTrue(actual, "No logo fill colors found")
        self.assertEqual(actual, documented,
                         "The README palette must describe the color and solid orange artwork")


if __name__ == "__main__":
    unittest.main()
