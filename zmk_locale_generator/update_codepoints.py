import json
import re
import unicodedata
import urllib.request
from dataclasses import dataclass
from importlib.metadata import Distribution
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .cldr import parse_cldr_keyboard
from .codepoints import (
    CodepointNames,
    CodepointNamesRaw,
    get_codepoint_names_raw,
    is_visible_character,
)
from .keyboards import get_keyboards

yaml = YAML()
yaml.version = (1, 2)

ROOT_PATH = Path(__file__).parent

KEYBOARDS_PATH = ROOT_PATH / "keyboards/keyboards.yaml"

UNICODE_BLOCKS_URL = "https://www.unicode.org/Public/UCD/latest/ucd/Blocks.txt"
UNICODE_BLOCK_RE = re.compile(r"^([0-9A-F]+)..([0-9A-F]+); (.+)")

YAML_HEADER = """\
# This document maps Unicode codepoints to key names for ZMK.
# Do not manually add new codepoints to this file. Instead, add entries to
# keyboards/keyboards.yaml and run scripts/update_codepoints.py. Then you can
# edit this file to assign names to the codepoints it adds.
#
# Each value is either a single string or a list of strings which must be valid
# C symbols. Each name will be prefixed with the locale abbreviation, e.g. for
# a German layout, A -> DE_A.
#
# If a name matches a name from ZMK's keys.h, any aliases for that key will
# automatically be added, e.g. for a German layout, ESCAPE -> DE_ESCAPE, DE_ESC.
"""


def upper_bound(seq, val, gt=lambda a, b: a > b):
    """
    Return the index of the first item in seq which is greater than val.
    """
    for i, x in enumerate(seq):
        if gt(x, val):
            return i
    return len(seq)


@dataclass
class UnicodeBlock:
    start: str
    end: str
    name: str


def get_unicode_blocks():
    """
    Get the list of blocks into which Unicode codepoints are grouped.
    """
    with urllib.request.urlopen(UNICODE_BLOCKS_URL) as response:
        text = response.read().decode("utf-8")
        for line in text.splitlines():
            if match := UNICODE_BLOCK_RE.match(line):
                start = chr(int(match.group(1), 16))
                end = chr(int(match.group(2), 16))
                name = match.group(3)
                yield UnicodeBlock(start=start, end=end, name=name)


def codepoint_to_block(c: str, blocks: list[UnicodeBlock]):
    """
    Get the UnicodeBlock containing a character.
    """
    return next(block for block in blocks if c >= block.start and c <= block.end)


def first_key(map: CodepointNames):
    return list(map.keys())[0]


def find_block(codepoints: CodepointNamesRaw, block: UnicodeBlock):
    """
    Find the YAML object for a Unicode block, creating it if necessary.
    """
    try:
        return next(b for b in codepoints if block.start <= first_key(b) <= block.end)
    except StopIteration:

        def compare_blocks(a: CommentedMap, b: UnicodeBlock):
            return first_key(a) > b.end

        index = upper_bound(codepoints, block, compare_blocks)
        item = CommentedMap()
        codepoints.insert(index, item)
        return item


def get_keyboard(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return parse_cldr_keyboard(f)


def get_used_codepoints():
    """
    Get the list of codepoints that are used by the selected keyboard layouts.
    """
    keyboards = [
        get_keyboard(keyboard.path) for keyboard in get_keyboards(KEYBOARDS_PATH)
    ]

    codepoints: set[str] = set()
    for keyboard in keyboards:
        for keymap in keyboard.keymaps:
            codepoints.update(keymap.keys.values())

    return sorted(codepoints)


def remove_unused_codepoints(codepoints: CodepointNamesRaw, used: list[str]):
    """
    Remove codepoints that are no longer used by any locale.
    """
    for block in codepoints:
        for c in list(block.keys()):
            if c not in used:
                del block[c]


def add_new_codepoint_placeholders(
    codepoints: CodepointNamesRaw, blocks: list[UnicodeBlock], used: list[str]
):
    """
    Add a placeholder name for new codepoints to indicate they need to be named.
    """
    for c in used:
        block = find_block(codepoints, codepoint_to_block(c, blocks))
        if c not in block:
            pos = upper_bound(block.keys(), c)
            block.insert(pos, c, "")


def add_codepoint_comments(codepoints: CodepointNamesRaw, blocks: list[UnicodeBlock]):
    """
    Add a comment to show the character for every printable character.
    """

    def get_char_comments(item: CodepointNames, c: str):
        yield c if is_visible_character(c) else "(non-printable)"

        if not item[c]:
            try:
                yield re.sub(r"[^\w]+", "_", unicodedata.name(c).upper())
            except ValueError:
                pass

    for i, item in enumerate(codepoints):
        block = codepoint_to_block(first_key(item), blocks)

        codepoints.yaml_set_comment_before_after_key(i, before="\n" + block.name)

        for c in item.keys():
            if comment := " ".join(get_char_comments(item, c)):
                item.yaml_add_eol_comment("# " + comment, key=c)


# Matches "foo:", "'foo':", or '"foo":' if preceded by "  " or "- ".
KEY_RE = re.compile(r"(?<=^(?:- |  ))(([\"']?).+\2)(?=:)")
COMMENT_PAD_RE = re.compile(" +#")


def transform(text: str):
    # ruamel.yaml will try to format keys in the shortest way possible, but we
    # want everything as Unicode escapes for consistency.
    # Also, because it shortens many keys but tries to keep comments on the same
    # column, it ends up padding out comments, so undo that.
    def escape_key(match):
        c = yaml.load(match.group(1))
        return f'"\\u{ord(c):04X}"'

    def transform_line(line: str):
        line = KEY_RE.sub(escape_key, line)
        line = COMMENT_PAD_RE.sub(" #", line)

        return line

    return "\n".join(transform_line(line) for line in text.splitlines())


def is_editable() -> bool:
    url = Distribution.from_name("zmk_locale_generator").read_text("direct_url.json")
    if not url:
        return False

    return bool(json.loads(url).get("dir_info", {}).get("editable", False))


def update_codepoints(path: Path | None = None):
    """
    Update the codepoints.yaml file at the given path, creating new keys for any
    key codes defined in CLDR data but not in the YAML file.

    If path is None, updates the codepoints.yaml file embedded withint the
    package. This requires the package be installed in editable mode.
    """

    if path is None:
        if not is_editable():
            raise RuntimeError(
                "Package is not installed in editable mode. An output path is required."
            )

        path = ROOT_PATH / "codepoints.yaml"

    blocks = list(get_unicode_blocks())
    codepoints = get_codepoint_names_raw()
    used = get_used_codepoints()

    remove_unused_codepoints(codepoints, used)
    add_new_codepoint_placeholders(codepoints, blocks, used)
    add_codepoint_comments(codepoints, blocks)

    codepoints.yaml_set_start_comment(YAML_HEADER)

    with path.open(mode="w", encoding="utf-8") as f:
        yaml.dump(codepoints, f, transform=transform)
        f.write("\n")
