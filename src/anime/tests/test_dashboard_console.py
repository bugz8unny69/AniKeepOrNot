"""Static checks for the CSP-compatible console theme."""
from pathlib import Path
import re

STATIC = Path(__file__).resolve().parents[1] / "dashboard" / "static"


def test_console_styles_use_palette_variables_and_flat_shapes():
    css = (STATIC / "dashboard.css").read_text()
    palette, rules = css.split("}", 1)
    for color in ("#0c0c0c", "#121212", "#d0d0d0", "#808080", "#333",
                  "#5fd75f", "#d7d75f", "#ff5f5f", "#5fd7ff"):
        assert color in palette
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rules)
    assert all(value.strip() == "0" for value in re.findall(r"border-radius:([^;}]+)", css))
    assert "gradient(" not in css and "box-shadow:" not in css
    assert "prefers-reduced-motion" in css


def test_console_assets_have_no_inline_styles_or_scripts():
    for name in ("index.html", "dashboard.js"):
        source = (STATIC / name).read_text()
        assert not re.search(r"\bstyle\s*=", source, re.I)
    html = (STATIC / "index.html").read_text()
    assert re.findall(r"<script[^>]*>(.*?)</script>", html, re.S) == [""]


def test_console_text_contrast_meets_aa_including_hover():
    css = (STATIC / "dashboard.css").read_text()
    palette = dict(re.findall(r"--([a-z]+):(#(?:[0-9a-f]{6}|[0-9a-f]{3}))\b", css))
    def luminance(color):
        value = color.lstrip('#')
        if len(value) == 3:
            value = ''.join(channel * 2 for channel in value)
        channels = [int(value[index:index+2],16)/255 for index in (0,2,4)]
        return sum(weight * (channel/12.92 if channel <= .04045 else ((channel+.055)/1.055)**2.4)
                   for weight,channel in zip((.2126,.7152,.0722),channels))
    for foreground in ('text','dim','ok','warn','bad','accent'):
        for background in ('bg','panel','hover'):
            contrast = (luminance(palette[foreground])+.05)/(luminance(palette[background])+.05)
            assert contrast >= 4.5, (foreground,background,contrast)
