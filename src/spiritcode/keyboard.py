"""Decode CSI-u keys before handing them to prompt-toolkit's key bindings."""

import re
from typing import Protocol, cast

from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding import KeyPress
from prompt_toolkit.keys import Keys

CSI_KEY = re.compile(r"\x1b\[(\d+)(?:;(\d+))?u\Z")
KEYPAD_TEXT = {
    **{57399 + digit: str(digit) for digit in range(10)},
    57409: ".",
    57410: "/",
    57411: "*",
    57412: "-",
    57413: "+",
    57415: "=",
    57416: ",",
}
KEYPAD_KEYS = {
    57414: Keys.ControlM,
    57417: Keys.Left,
    57418: Keys.Right,
    57419: Keys.Up,
    57420: Keys.Down,
    57421: Keys.PageUp,
    57422: Keys.PageDown,
    57423: Keys.Home,
    57424: Keys.End,
    57425: Keys.Insert,
    57426: Keys.Delete,
}


class ParserInput(Protocol):
    vt100_parser: Vt100Parser


class KeyboardParser(Vt100Parser):
    def _get_match(self, prefix: str) -> Keys | tuple[Keys, ...] | None:
        if CSI_KEY.fullmatch(prefix):
            return Keys.Any
        return super()._get_match(prefix)

    def _call_handler(self, key: str | Keys | tuple[Keys, ...], insert_text: str) -> None:
        match = CSI_KEY.fullmatch(insert_text)
        if key != Keys.Any or match is None:
            super()._call_handler(key, insert_text)
            return
        codepoint = int(match[1])
        modifiers = int(match[2] or "1") - 1
        shift, alt, control = bool(modifiers & 1), bool(modifiers & 2), bool(modifiers & 4)
        decoded: str | Keys
        text = ""
        if codepoint == 13:
            decoded = Keys.ControlO if shift else Keys.ControlM
        elif codepoint == 9:
            decoded = Keys.BackTab if shift else Keys.ControlI
        elif codepoint in KEYPAD_KEYS:
            decoded = Keys.ControlO if codepoint == 57414 and shift else KEYPAD_KEYS[codepoint]
        elif codepoint in KEYPAD_TEXT:
            decoded = text = KEYPAD_TEXT[codepoint]
        elif 57344 <= codepoint <= 63743 or codepoint > 0x10FFFF:
            # Unsupported functional keys must never leak their escape codes.
            return
        else:
            text = chr(codepoint)
            if control and 97 <= codepoint <= 122:
                text = chr(codepoint - 96)
            elif control and 64 <= codepoint <= 95:
                text = chr(codepoint - 64)
            elif control and codepoint == 32:
                text = "\x00"
            elif control and codepoint == 63:
                text = "\x7f"
            elif shift and text.isascii() and text.isalpha():
                text = text.upper()
            mapped = ANSI_SEQUENCES.get(text)
            if isinstance(mapped, Keys):
                decoded = mapped
            elif len(text) == 1 and text.isprintable():
                decoded = text
            else:
                return
        if alt:
            self.feed_key_callback(KeyPress(Keys.Escape, "\x1b"))
        self.feed_key_callback(KeyPress(decoded, text))


def prepare_keyboard(input_source: object) -> bool:
    parser = getattr(input_source, "vt100_parser", None)
    if isinstance(parser, KeyboardParser):
        return True
    if isinstance(parser, Vt100Parser):
        cast(ParserInput, input_source).vt100_parser = KeyboardParser(parser.feed_key_callback)
        return True
    return False
