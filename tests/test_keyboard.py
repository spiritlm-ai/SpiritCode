"""Layout-independent terminal shortcuts must reach the existing key bindings."""

import pytest
from prompt_toolkit.keys import Keys

from spiritcode.keyboard import KeyboardParser


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        ("\x1b[1089;5u", Keys.ControlC),
        ("\x1b[1057;5u", Keys.ControlC),
        ("\x1b[1092;5u", Keys.ControlA),
        ("\x1b[1074;5u", Keys.ControlD),
        ("\x1b[1075;5u", Keys.ControlU),
        ("\x1b[1094;5u", Keys.ControlW),
        ("\x1b[1089::99;5u", Keys.ControlC),
        ("\x1b[1089:1057:99;6u", Keys.ControlC),
        ("\x1b[945::99;5u", Keys.ControlC),
        ("\x1b[1489::97;5u", Keys.ControlA),
        ("\x1b[1094::119;5:2u", Keys.ControlW),
    ],
)
def test_control_shortcuts_do_not_insert_layout_characters(sequence, expected):
    keys = []
    parser = KeyboardParser(keys.append)
    for character in sequence:
        parser.feed(character)
    parser.flush()
    assert [key.key for key in keys] == [expected]
    assert keys[0].data == chr(ord(expected.value[-1]) - ord("a") + 1)


def test_unmodified_unicode_and_bracketed_paste_remain_text():
    keys = []
    parser = KeyboardParser(keys.append)
    parser.feed("с\x1b[1089u\x1b[1089::99u")
    parser.feed("\x1b[200~\x1b[1089::99;5u\x1b[201~")
    parser.flush()
    assert [key.key for key in keys] == ["с", "с", "с", Keys.BracketedPaste]
    assert keys[-1].data == "\x1b[1089::99;5u"


def test_alt_shortcuts_shifted_text_and_release_events():
    keys = []
    parser = KeyboardParser(keys.append)
    parser.feed("\x1b[1080;3u\x1b[945::98;3u\x1b[1089:1057;2u")
    parser.feed("\x1b[1089::99;5:3u")
    parser.flush()
    assert [key.key for key in keys] == [Keys.Escape, "b", Keys.Escape, "b", "С"]


def test_legacy_control_keys_and_escape_sequences_still_work():
    keys = []
    parser = KeyboardParser(keys.append)
    parser.feed("\x03\x01\x1b[D\x1bb\x1b")
    parser.flush()
    assert [key.key for key in keys] == [
        Keys.ControlC,
        Keys.ControlA,
        Keys.Left,
        Keys.Escape,
        "b",
        Keys.Escape,
    ]


@pytest.mark.parametrize("sequence", ["\x1b[1089;5u", "\x1b[1089::99;5u"])
async def test_russian_control_c_interrupts_the_chat_instead_of_inserting_text(sequence):
    import asyncio

    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from spiritcode.ui import UI, input_bindings

    ui = UI(color=False)
    with create_pipe_input() as pipe:
        ui.prompt = PromptSession(
            input=pipe, output=DummyOutput(), key_bindings=input_bindings(ui), multiline=True
        )
        pipe.send_text("текст" + sequence)
        with pytest.raises(KeyboardInterrupt):
            await asyncio.wait_for(ui.ask(chat=True), timeout=2)
        assert not ui.interrupted_empty
        assert ui.prompt.default_buffer.text == "текст"


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        ("\x1b[1092;5u\x1b[1083;5u", ""),  # Ctrl+A, Ctrl+K
        ("\x1b[1075;5u", ""),  # Ctrl+U
        ("\x1b[1094;5u", "первое "),  # Ctrl+W
    ],
)
async def test_russian_editing_shortcuts_use_existing_chat_bindings(sequence, expected):
    import asyncio

    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from spiritcode.ui import UI, input_bindings

    ui = UI(color=False)
    with create_pipe_input() as pipe:
        ui.prompt = PromptSession(
            input=pipe, output=DummyOutput(), key_bindings=input_bindings(ui), multiline=True
        )
        pipe.send_text("первое второе" + sequence + "готово\r")
        assert await asyncio.wait_for(ui.ask(chat=True), timeout=2) == expected + "готово"
