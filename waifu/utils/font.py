"""Unicode "fonts" for player names and headings (``/font``, parity with the old bot).

The reference bot shipped ``font.py`` with a hand-written ``str.maketrans`` table per
style; three of them silently dropped characters outside the BMP (any emoji, any
non-Latin letter), which is how a Japanese display name turned into ``???`` in
/harem. The mapping below is built from codepoint *ranges* instead of a literal table,
so unmapped characters pass through untouched — the correct degradation for a
decorative transform.

These are not fonts: they are Mathematical Alphanumeric Symbols. That has two real
consequences, both handled here:

* screen readers announce "mathematical bold small a" — so this must never be used
  inside a bot's own instructions, only in player-chosen names;
* they cannot be searched by text and break ``@username`` matching, so usernames are
  always stored raw and only *displayed* styled (:func:`style` is not a persistence
  helper on purpose).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Unicode base for each letter class, per style: (upper, lower). ``None`` = unmapped.
_RANGES: dict[str, list[tuple[int | None, int | None]]] = {
    # style name → list of (base_upper, base_lower) for A-Z / a-z
    "mono": [(0x1D670 - 0x41, 0x1D68A - 0x61)],
    "fraktur": [(0x1D56C - 0x41, 0x1D586 - 0x61)],
    "bold": [(0x1D400 - 0x41, 0x1D41A - 0x61)],
    "italic": [(0x1D434 - 0x41, 0x1D44E - 0x61)],
    "script": [(0x1D49C - 0x41, 0x1D4B6 - 0x61)],
    "double": [(0x1D538 - 0x41, 0x1D552 - 0x61)],
}
#: Letter holes where Unicode simply has no codepoint; those fall back to ASCII.
_HOLES = {
    "bold": {0x42: 0x1D401 - 0x42, 0x62: 0x1D41B - 0x62},  # B/b live in letterlike symbols
    "italic": {0x42: 0x210C - 0x42, 0x62: 0x210E - 0x62},
    "script": {
        0x42: 0x212C - 0x42,
        0x45: 0x2130 - 0x45,
        0x46: 0x2131 - 0x46,
        0x48: 0x210B - 0x48,
        0x49: 0x2110 - 0x49,
        0x4C: 0x2112 - 0x4C,
        0x4D: 0x2133 - 0x4D,
        0x52: 0x211B - 0x52,
        0x64: 0x2147 - 0x64,
        0x65: 0x2148 - 0x65,
        0x66: 0x2149 - 0x66,
        0x6A: 0x212F - 0x6A,
        0x6D: 0x2134 - 0x6D,
    },
    "fraktur": {
        0x43: 0x212D - 0x43,
        0x48: 0x210C - 0x48,
        0x49: 0x2111 - 0x49,
        0x52: 0x211C - 0x52,
        0x5A: 0x2128 - 0x5A,
    },
    "double": {0x48: 0x210D - 0x48},
}
_DIGITS = {
    "mono": 0x1D7F6 - 0x30,
    "bold": 0x1D7CE - 0x30,
    "double": 0x1D7D8 - 0x30,
}


@dataclass(frozen=True, slots=True)
class Font:
    key: str
    label: str
    sample: str

    @property
    def known(self) -> bool:
        return self.key in _RANGES


FONTS: tuple[Font, ...] = (
    Font("default", "Default", "Waifu Bot"),
    Font("mono", "Monospace", "Waifu Bot"),
    Font("fraktur", "Gothic", "Waifu Bot"),
    Font("script", "Cursive", "Waifu Bot"),
    Font("double", "Double-struck", "Waifu Bot"),
    Font("bold", "Bold", "Waifu Bot"),
    Font("italic", "Italic", "Waifu Bot"),
)
BY_KEY = {font.key: font for font in FONTS}
#: ``config.DEFAULT_FONT`` was "mono"; kept so a migrated profile looks the same.
DEFAULT = "mono"


def style(text: str, key: str = DEFAULT) -> str:
    """Return ``text`` in the requested decorative style (never raises)."""
    if not text or key not in _RANGES or key == "default":
        return text
    upper_shift, lower_shift = _RANGES[key][0]
    holes = _HOLES.get(key, {})
    digits = _DIGITS.get(key)
    out: list[str] = []
    for char in text:
        code = ord(char)
        if code in holes:
            out.append(chr(code + holes[code]))
        elif 0x41 <= code <= 0x5A:
            out.append(chr(code + upper_shift))
        elif 0x61 <= code <= 0x7A:
            out.append(chr(code + lower_shift))
        elif digits is not None and 0x30 <= code <= 0x39:
            out.append(chr(code + digits))
        else:
            out.append(char)  # emoji, CJK, punctuation: pass through, never dropped
    return "".join(out)


_REVERSE: dict[str, str] = {}


def _reverse_map() -> dict[str, str]:
    """Decorative → ASCII, derived from :func:`style` so the two cannot drift."""
    if not _REVERSE:
        for key in _RANGES:
            for code in list(range(0x41, 0x5B)) + list(range(0x61, 0x7B)) + list(range(0x30, 0x3A)):
                char = chr(code)
                mapped = style(char, key)
                if mapped != char:
                    _REVERSE[mapped] = char
    return _REVERSE


def strip_style(text: str) -> str:
    """Map decorative letters back to ASCII (for search and ``@username`` matching)."""
    if not text:
        return text
    table = _reverse_map()
    return "".join(table.get(char, char) for char in text)


def label(key: str) -> str:
    font = BY_KEY.get(key)
    return font.label if font else key
