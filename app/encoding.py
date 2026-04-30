"""Convert engine `&X` colour codes to HTML spans for the screen output panel.

The engine writes ANSI-style colour escapes as `&` + single letter; we
translate to inline `<span class="cN">` so CSS does the actual styling.
Unknown sequences are stripped to avoid leaking them into the page.
"""

from __future__ import annotations

import html
import re

# Bylins palette (subset). Maps the letter after `&` to a CSS class name.
# `&n` resets; we map it to a closing span. `q`/`Q` are bold on/off in some
# clients; here we treat them as no-op to keep markup simple.
_COLOUR_MAP = {
    "K": "ck",  # black
    "R": "cR",  # bright red
    "G": "cG",  # bright green
    "Y": "cY",  # bright yellow
    "B": "cB",  # bright blue
    "M": "cM",  # bright magenta
    "C": "cC",  # bright cyan
    "W": "cW",  # white
    "r": "cr",  # red
    "g": "cg",  # green
    "y": "cy",  # yellow
    "b": "cb",  # blue
    "m": "cm",  # magenta
    "c": "cc",  # cyan
    "w": "cw",  # grey/dim white
}

_TOKEN = re.compile(r"&(.)")


def colour_to_html(text: str) -> str:
    """Translate `&X` codes to HTML spans, escape everything else."""
    out: list[str] = []
    open_spans = 0
    pos = 0
    for match in _TOKEN.finditer(text):
        out.append(html.escape(text[pos : match.start()]))
        code = match.group(1)
        if code == "n":
            out.append("</span>" * open_spans)
            open_spans = 0
        elif code in ("q", "Q"):
            pass  # ignore bold-on / bold-off for now
        elif code in _COLOUR_MAP:
            out.append(f'<span class="{_COLOUR_MAP[code]}">')
            open_spans += 1
        # Unknown -- silently drop the `&X` token (also dropped from output).
        pos = match.end()
    out.append(html.escape(text[pos:]))
    out.append("</span>" * open_spans)
    return "".join(out)
