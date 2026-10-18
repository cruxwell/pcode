"""Plain ASCII stand-ins for the symbols pcode draws, for terminals that can't draw them.

Every stand-in takes the single cell its symbol does, so ASCII mode is just
another encoding of the same screen: layout math, row counts and cursor
movement don't change. That is why it is applied as stdout's codec, the one
place every writer meets (prompt_toolkit, Rich, popups, scrollback), rather
than by editing the symbols where each module draws them. Rich also reads the
encoding's name and draws ASCII table borders by itself.

Only symbols are replaced. Letters in any script pass through untouched.
"""

import codecs
import io
import os
import sys
import unicodedata
from collections.abc import Mapping

from rich.cells import cell_len

CODEC = "pcode-ascii"

# Symbols outside the ranges `_by_name` sorts out.
_SYMBOLS = {
    "✓": "v",
    "✔": "v",
    "√": "v",
    "✗": "x",
    "✘": "x",
    "×": "x",
    "⊘": "/",
    "•": "*",
    "·": "-",
    "…": ".",
    "⋯": ":",
    "⋮": ":",
    "❝": '"',
    "›": ">",
    "❯": ">",
    "»": ">",
    "«": "<",
    "⟳": "~",
    "⌕": "?",
    "✎": "#",
    "⎘": "=",
    "⧖": "%",
    "−": "-",
    "–": "-",
    "—": "-",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
}

# Unicode blocks drawn from by name: box drawing, block elements, geometric
# shapes, arrows and braille (spinners).
_RANGES = ((0x2190, 0x21FF), (0x2500, 0x25FF), (0x2800, 0x28FF))


def _by_name(char: str) -> str | None:
    """A stand-in read off the character's Unicode name, or None to keep it."""
    name = unicodedata.name(char, "")
    words = set(name.replace("-", " ").split())
    if name.startswith("BRAILLE"):
        return " " if char == "\u2800" else "."
    if "DIAGONAL" in words:
        return "X" if "CROSS" in words else "/" if "UPPER RIGHT TO LOWER LEFT" in name else "\\"
    if name.startswith("BOX DRAWINGS"):
        across = words & {"HORIZONTAL", "LEFT", "RIGHT"}
        down = words & {"VERTICAL", "UP", "DOWN"}
        return "+" if across and down else "-" if across else "|"
    if "ARROW" in words or "POINTING" in words:
        if "CLOCKWISE" in words or "ANTICLOCKWISE" in words:
            return "~"
        for word, stand_in in (("RIGHT", ">"), ("LEFT", "<"), ("UP", "^"), ("DOWN", "v")):
            if f"{word}WARDS" in words or word in words:
                # `↵` (downwards with corner leftwards) reads as a return.
                return "<" if "CORNER" in words else stand_in
        return ">"
    if "BLOCK" in words or "SHADE" in words:
        # `▌`, the quote rail, and the other left/right slivers read as a bar.
        return "|" if words & {"LEFT", "RIGHT"} else "#"
    if "CIRCLE" in words or "ARC" in words:
        return "*" if "BLACK" in words else "o"
    return "*"


def _table() -> dict[int, str]:
    table = {ord(char): stand_in for char, stand_in in _SYMBOLS.items()}
    for start, end in _RANGES:
        for point in range(start, end + 1):
            char = chr(point)
            # A wide symbol would need two stand-ins; those stay as drawn.
            if point not in table and unicodedata.name(char, "") and cell_len(char) == 1:
                stand_in = _by_name(char)
                if stand_in is not None:
                    table[point] = stand_in
    return table


TABLE = _table()


def to_ascii(text: str) -> str:
    return text.translate(TABLE)


def _search(name: str) -> codecs.CodecInfo | None:
    """`pcode-ascii-<base>`: the stand-ins, then the stream's own encoding.

    Wrapping the base keeps a legacy locale's letters (`é` in Latin-1, kana
    in EUC-JP) encoded as its terminal expects. The name never starts with
    `utf`, which is what tells Rich to draw ASCII boxes.
    """
    prefix = CODEC.replace("-", "_") + "_"
    name = name.replace("-", "_")
    if not name.startswith(prefix):
        return None
    base = codecs.lookup(name.removeprefix(prefix))

    class Encoder(codecs.IncrementalEncoder):
        def __init__(self, errors: str = "strict"):
            super().__init__(errors)
            self.base = base.incrementalencoder(errors)

        def encode(self, input: str, final: bool = False) -> bytes:
            self.base.errors = self.errors
            return self.base.encode(input.translate(TABLE), final)

        def reset(self) -> None:
            self.base.reset()

    def encode(text: str, errors: str = "strict") -> tuple[bytes, int]:
        return base.encode(text.translate(TABLE), errors)[0], len(text)

    return codecs.CodecInfo(
        name=f"{CODEC}-{base.name}",
        encode=encode,
        decode=base.decode,
        incrementalencoder=Encoder,
        incrementaldecoder=base.incrementaldecoder,
        streamreader=base.streamreader,
        streamwriter=base.streamwriter,
    )


codecs.register(_search)

_active = False


def active() -> bool:
    """Whether stdout is drawing ASCII stand-ins."""
    return _active


def legacy_locale(env: Mapping[str, str]) -> bool:
    """Whether the locale names a character set other than UTF-8 (`en_US.ISO-8859-1`).

    A bare `C`, `POSIX` or unset locale says nothing about the terminal (a
    container's default), so only an explicit other charset counts.
    """
    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = env.get(key)
        if value:
            charset = value.partition(".")[2].partition("@")[0]
            return bool(charset) and charset.lower().replace("-", "") != "utf8"
    return False


def wanted(setting: str, env: Mapping[str, str] | None = None, wide: bool | None = None) -> bool:
    """Whether the `glyphs` setting (`auto`, `unicode`, `ascii`) asks for ASCII.

    `wide` is the startup probe's answer: the terminal drew a box-drawing
    line two cells wide, which shifts every row that holds one.
    """
    if setting in ("ascii", "unicode"):
        return setting == "ascii"
    env = os.environ if env is None else env
    # The Linux console's fonts lack most of these symbols. Other bare `TERM`s
    # (`vt100`, `ansi`) are as often a Unicode terminal misreporting itself.
    return env.get("TERM", "") == "linux" or legacy_locale(env) or bool(wide)


def apply(on: bool, stream=None) -> bool:
    """Switch the terminal stream (stdout) to ASCII stand-ins; whether it now draws them.

    Only a terminal is switched: a reply piped elsewhere keeps its symbols.
    """
    global _active
    stream = sys.stdout if stream is None else stream
    if not on or not isinstance(stream, io.TextIOWrapper) or not stream.isatty():
        return _active
    stream.flush()
    if not stream.encoding.startswith(CODEC):
        # Keep the error handler: UTF-8 mode's `surrogateescape` lets a file
        # name that isn't UTF-8 print rather than raise.
        stream.reconfigure(encoding=f"{CODEC}-{stream.encoding}", errors=stream.errors)
    _active = True
    return True
