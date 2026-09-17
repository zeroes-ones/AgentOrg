#!/usr/bin/env python3
"""frontmatter.py — parse SKILL.md YAML frontmatter strictly and safely.

WHY THIS EXISTS
---------------
Every skill's contract lives in YAML frontmatter: its `workflow:` block supplies the
completion criteria that gate every phase transition, its `chain:` block supplies the
handoff graph, and its `token_budget` drives the prompt assembly. Mis-parsing that block
does not fail loudly — it produces a skill that looks like it has no criteria, and a node
that can then declare itself done without evidence.

So the parser is deliberately narrow and loud, in that order:

- **A documented subset, not general YAML.** Only the shapes the library actually uses are
  accepted. Anything else raises with a line number, because silently ignoring an
  unrecognised construct is exactly how a criteria list goes missing.
- **PyYAML is used when available, but only as a fast path.** The engine keeps its
  no-runtime-dependency property, so the stdlib parser is the fallback and is fully
  tested; PyYAML is a convenience that must agree with it.
- **No `yaml.load`.** If PyYAML is used it is `safe_load` only — frontmatter from a
  third-party repo must never be able to construct arbitrary Python objects.

Usage:
    front, body = parse_frontmatter(skill_text)
    front["workflow"]["completion"]["criteria"]
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["FrontmatterError", "parse_frontmatter", "split_document", "safe_load_subset"]


class FrontmatterError(ValueError):
    """Raised when frontmatter is absent, unterminated, or outside the accepted subset."""

    def __init__(self, message: str, *, line: int | None = None) -> None:
        self.line = line
        where = f" (frontmatter line {line})" if line is not None else ""
        super().__init__(message + where)


# A document is `---\n ... \n---\n body`. The closing delimiter must be on its own line.
_FRONT_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
# Characters YAML reserves at the start of a scalar. Rejected explicitly so a stray one is
# reported rather than silently becoming part of a string.
_RESERVED_LEAD = ("&", "*", "%", "@", "`")


def split_document(text: str) -> tuple[str, str]:
    """Split a document into ``(frontmatter_text, body)``.

    Raises
    ------
    FrontmatterError
        When there is no frontmatter block, or the opening delimiter is never closed —
        the latter is a truncated file, which must not be read as "no frontmatter".
    """
    if not text.startswith("---"):
        raise FrontmatterError("document does not begin with a '---' frontmatter delimiter")
    match = _FRONT_RE.match(text)
    if match is None:
        raise FrontmatterError(
            "frontmatter opening delimiter was never closed with a matching '---' line; "
            "the file looks truncated"
        )
    return match.group(1), text[match.end():]


def _strip_comment(value: str) -> str:
    """Remove a trailing ` # comment` that is outside quotes.

    A `#` inside a quoted scalar is content, not a comment — and skill descriptions do
    contain them, so this tracks quote state rather than splitting on the first `#`.
    """
    quote: str | None = None
    for index, char in enumerate(value):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _scalar(raw: str, *, line: int) -> Any:
    """Interpret a scalar string as a Python value.

    Only the types the library uses: str, int, float, bool, null, and flow lists of
    scalars. Everything else stays a string rather than being guessed at.
    """
    text = _strip_comment(raw).strip()
    if text == "":
        return None
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        quote = text[0]
        inner = text[1:-1]
        if quote == '"':
            # Minimal escape handling: the escapes that appear in real descriptions.
            inner = inner.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
        else:
            inner = inner.replace("''", "'")
        return inner
    if text.startswith(_RESERVED_LEAD):
        raise FrontmatterError(
            f"unsupported YAML construct starting with {text[0]!r}: anchors, aliases, tags "
            "and directives are outside the accepted subset",
            line=line,
        )
    # `!!str`, `!custom` and friends: a tag is not a value, and silently letting PyYAML
    # resolve one would mean the two parser paths disagree about what the document says.
    if text.startswith("!"):
        raise FrontmatterError(
            f"unsupported YAML tag {text.split()[0]!r}: tags are outside the accepted subset",
            line=line,
        )
    if text.startswith("[") and text.endswith("]"):
        return _flow_list(text, line=line)
    lowered = text.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~", "none"):
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def _flow_list(text: str, *, line: int) -> list[Any]:
    """Parse a scalar-only flow list: `[a, b, c]`.

    Splitting tracks quote state so a comma inside a quoted item does not split it — which
    happens in practice, e.g. a criterion reading "reduces p95 from 340ms to 120ms, ±15ms".
    """
    inner = text[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in inner:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            continue
        if char == ",":
            items.append("".join(current))
            current = []
            continue
        current.append(char)
    items.append("".join(current))
    return [_scalar(item, line=line) for item in items]


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _is_skippable(line: str) -> bool:
    return not line.strip() or line.lstrip().startswith("#")


def _parse_block(lines: list[str], start: int, indent: int) -> tuple[Any, int]:
    """Parse a mapping or sequence at `indent`, returning ``(value, next_index)``.

    Recursive rather than a stack machine: the subset the library uses is shallow, and
    recursion makes the nesting rules obvious — which matters because a mis-nested
    criteria list would silently produce a node with no completion criteria.

    The container type is decided by the first significant line: `- ` means a sequence,
    anything else means a mapping.
    """
    index = start
    # Find the first significant line to decide the container type.
    while index < len(lines) and _is_skippable(lines[index]):
        index += 1
    if index >= len(lines) or _indent_of(lines[index]) < indent:
        return None, start
    is_sequence = lines[index].lstrip().startswith("- ")

    result: Any = [] if is_sequence else {}
    while index < len(lines):
        raw = lines[index]
        if _is_skippable(raw):
            index += 1
            continue
        current = _indent_of(raw)
        if current < indent:
            break
        if current > indent:
            # Deeper content is consumed by the key/item that owns it; reaching here means
            # the indentation is inconsistent, which must be reported rather than guessed.
            raise FrontmatterError(
                f"unexpected indentation: line is deeper than its parent key "
                f"({current} > {indent}): {raw.strip()[:50]!r}",
                line=index + 1,
            )
        stripped = raw.lstrip()
        # A sequence may legitimately appear at the container's own indent, in which case it
        # belongs to the mapping being built rather than to the previous key. Detect that
        # before treating it as an out-of-place item.
        if stripped.startswith("- ") and not is_sequence:
            break

        if is_sequence:
            if not stripped.startswith("- "):
                break
            item_text = stripped[2:].strip()
            item_line = index + 1
            index += 1
            if item_text and ":" in item_text and not item_text.startswith(("'", '"')):
                # A mapping inside a sequence item: the item's own keys may continue on the
                # following, more-indented lines.
                key, _, value = item_text.partition(":")
                item: dict[str, Any] = {}
                if value.strip():
                    item[key.strip()] = _scalar(value, line=item_line)
                else:
                    child, index = _parse_block(lines, index, indent + 1)
                    item[key.strip()] = child if child is not None else None
                # Consume any further keys belonging to this item. The item's own keys sit
                # deeper than the `- ` marker; a key at or below the marker's indent starts
                # a new item or a new top-level key instead.
                while index < len(lines):
                    if _is_skippable(lines[index]):
                        index += 1
                        continue
                    child_indent = _indent_of(lines[index])
                    if child_indent <= indent:
                        break
                    child_line = lines[index]
                    if child_line.lstrip().startswith("- "):
                        # A nested sequence directly under the item's last key.
                        nested, index = _parse_block(lines, index, child_indent)
                        if item:
                            last_key = next(reversed(item))
                            item[last_key] = nested
                        continue
                    ckey, _, cvalue = child_line.lstrip().partition(":")
                    child_line_no = index + 1
                    index += 1
                    cvalue = _strip_comment(cvalue).strip()
                    if cvalue[:1] in ("'", '"') and not _quote_closed(cvalue):
                        cvalue, index = _read_multiline_quoted(lines, index, cvalue)
                    if cvalue in (">", "|", ">-", "|-", ">+", "|+"):
                        block, index = _read_block_scalar(
                            lines, index, child_indent, folded=cvalue.startswith(">"))
                        item[ckey.strip()] = block
                    elif cvalue == "":
                        # A nested mapping (e.g. `changes:` followed by deeper keys) or a
                        # sequence. YAML allows either, at the child's indent or deeper.
                        nested, index = _parse_child(lines, index, child_indent)
                        item[ckey.strip()] = nested
                    else:
                        item[ckey.strip()] = _scalar(cvalue, line=child_line_no)
                result.append(item)
                continue
            result.append(_scalar(item_text, line=item_line))
            continue

        # Mapping.
        if stripped.startswith("- "):
            break
        if ":" not in stripped:
            raise FrontmatterError(
                f"line is neither a 'key: value' mapping nor a sequence item: {stripped[:55]!r}",
                line=index + 1,
            )
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = _strip_comment(value).strip()
        line_no = index + 1
        index += 1

        # A quoted scalar may span lines: the library uses this for long `description`
        # fields, with the closing quote on its own line. Detecting the open quote here and
        # consuming until it closes is what makes those skills parse at all.
        if value[:1] in ("'", '"') and not _quote_closed(value):
            value, index = _read_multiline_quoted(lines, index, value)
        elif value and value not in (">", "|", ">-", "|-", ">+", "|+"):
            # A plain scalar may also continue on following, more-indented lines. YAML folds
            # those into spaces, and the library uses this form for long descriptions:
            #     description: Use when building pipelines,
            #       passing state between agents.
            # Without this, most real skills would fail to parse.
            value, index = _read_plain_continuation(lines, index, value, indent)

        if value in (">", "|", ">-", "|-", ">+", "|+"):
            block, index = _read_block_scalar(lines, index, indent, folded=value.startswith(">"))
            result[key] = block
        elif value == "":
            # A key with no inline value is followed either by a mapping (deeper indent) or
            # by a block sequence. YAML permits that sequence at the *same* indentation as
            # its parent key:
            #     tags:
            #     - a
            #     - b
            # Requiring a deeper indent here silently drops nearly every real frontmatter
            # block, so the sequence indent is discovered from the next significant line.
            child, index = _parse_child(lines, index, indent)
            result[key] = child
        else:
            result[key] = _scalar(value, line=line_no)

    return result, index


def _quote_closed(value: str) -> bool:
    """True when a quoted scalar opens and closes on the same line."""
    if not value or value[0] not in ("'", '"'):
        return True
    quote = value[0]
    if len(value) < 2:
        return False
    # Scan for an unescaped closing quote after the first character.
    index = 1
    while index < len(value):
        char = value[index]
        if char == "\\" and quote == '"':
            index += 2
            continue
        if char == quote:
            # A doubled quote is an escaped quote, not the terminator.
            if quote == "'" and index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            return True
        index += 1
    return False


def _read_plain_continuation(lines: list[str], start: int, first: str, key_indent: int
                             ) -> tuple[str, int]:
    """Fold a plain multi-line scalar's continuation lines into the first, returning
    ``(text, next_index)``.

    Continuation lines are those indented deeper than the key. Folding replaces the line
    break with a single space and turns a genuinely blank line into a newline, matching
    YAML's flow-folding rule. Stopping at the first line at or below the key's indent keeps
    the following keys intact — over-consuming here would swallow the whole frontmatter.
    """
    pieces: list[str] = [first]
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            # A blank line inside a plain scalar folds to a newline, but only if the scalar
            # continues afterwards; look ahead rather than committing.
            probe = index + 1
            while probe < len(lines) and not lines[probe].strip():
                probe += 1
            if probe < len(lines) and _indent_of(lines[probe]) > key_indent:
                pieces.append("\n")
                index = probe
                continue
            # The blank line ends the scalar. YAML preserves that final line break, and
            # dropping it would make the strict parser disagree with PyYAML by one
            # character on nearly every skill.
            if pieces and pieces[-1] != "\n":
                pieces.append("\n")
            break
        if _indent_of(line) <= key_indent:
            # A plain multi-line scalar ends with a line break, which YAML preserves. Without
            # this the strict parser would differ from PyYAML by one trailing newline on
            # nearly every skill description.
            if pieces and pieces[-1] != "\n":
                pieces.append("\n")
            break
        if line.lstrip().startswith("#"):
            index += 1
            continue
        pieces.append(line.strip())
        index += 1

    rendered = ""
    for piece in pieces:
        if piece == "\n":
            rendered += "\n"
        elif rendered and not rendered.endswith(("\n", " ")):
            rendered += " " + piece
        else:
            rendered += piece
    return rendered, index


def _read_multiline_quoted(lines: list[str], start: int, first: str) -> tuple[str, int]:
    """Read a quoted scalar spanning several lines, returning ``(text, next_index)``.

    YAML folds line breaks inside a multi-line quoted scalar into spaces and an empty line
    into a newline. Preserving that matters because these fields are the skill descriptions
    used to route work; collapsing them incorrectly would change the meaning of routing text.

    The library's shape is a quote opened on the key's line and closed on its own line
    several lines later, often with a blank line before the close. Treating that blank line
    as content (and the lone closing quote as text) would swallow every following key, so
    the closing quote is detected exactly.
    """
    quote = first[0]
    pieces: list[str] = [first[1:]]
    index = start
    while index < len(lines):
        line = lines[index]
        index += 1
        stripped = line.strip()

        # A line that is *only* the closing quote ends the scalar.
        if stripped in (quote, quote + quote):
            break
        # A line whose content closes the quote (trailing text after the close is a YAML
        # error, so anything after it is ignored deliberately).
        if stripped.endswith(quote) and _quote_closed(quote + stripped):
            pieces.append(stripped[: -len(quote)])
            break
        if not stripped:
            # A meaningful blank line inside the scalar folds to a newline, but only when
            # more content follows. A trailing blank line before the close is just spacing.
            probe = index
            while probe < len(lines) and not lines[probe].strip():
                probe += 1
            if probe < len(lines) and lines[probe].strip() not in (quote, quote + quote):
                pieces.append("\n")
            continue
        if stripped.startswith("#"):
            continue
        pieces.append(stripped)

    rendered = ""
    for piece in pieces:
        if piece == "\n":
            rendered += "\n"
        elif rendered and not rendered.endswith(("\n", " ")):
            rendered += " " + piece
        else:
            rendered += piece
    return quote + rendered.strip() + quote, index


def _parse_child(lines: list[str], index: int, parent_indent: int) -> tuple[Any, int]:
    """Parse the value that follows a key with no inline value.

    Returns ``(value, next_index)``. A block sequence may sit at the parent's own indent or
    deeper; a mapping must be deeper. When the following line belongs to the parent instead
    (same indent, not a sequence item), the value is None and nothing is consumed.
    """
    probe = index
    while probe < len(lines) and _is_skippable(lines[probe]):
        probe += 1
    if probe >= len(lines):
        return None, index

    line_indent = _indent_of(lines[probe])
    if lines[probe].lstrip().startswith("- ") and line_indent >= parent_indent:
        # A sequence at the parent's indent or deeper.
        return _parse_block(lines, probe, line_indent)
    if line_indent > parent_indent:
        return _parse_block(lines, probe, line_indent)
    return None, index


def _assert_subset_safe(text: str) -> None:
    """Reject constructs outside the documented subset, whichever parser would run.

    Called before either parser so the two paths cannot disagree: PyYAML resolves an anchor
    or a tag silently, which would mean the fallback parser accepts documents the fast path
    interprets differently. Validating up front makes the subset the contract, not a
    property of whichever parser happened to run.
    """
    for number, line in enumerate(text.splitlines(), start=1):
        head = line[: _indent_of(line)]
        if "\t" in head:
            raise FrontmatterError(
                "tab character used for indentation; YAML forbids tabs and the resulting "
                "structure would be silently wrong",
                line=number,
            )
        content = line.strip()
        if not content or content.startswith("#"):
            continue
        if content.startswith(("&", "!!")):
            raise FrontmatterError(
                f"unsupported YAML construct {content.split()[0]!r}: anchors and tags are "
                "outside the accepted subset",
                line=number,
            )
        if ":" in content:
            key_part = content.partition(":")[0].strip()
            if key_part.startswith(("&", "!")):
                raise FrontmatterError(
                    f"unsupported YAML construct on key {key_part!r}: anchors and tags are "
                    "outside the accepted subset",
                    line=number,
                )
            value_part = content.partition(":")[2].strip()
            if value_part.startswith("&"):
                raise FrontmatterError(
                    f"unsupported YAML anchor {value_part.split()[0]!r}: anchors are outside "
                    "the accepted subset",
                    line=number,
                )
            if value_part.startswith("!") and value_part not in ("!", "!!"):
                raise FrontmatterError(
                    f"unsupported YAML tag {value_part.split()[0]!r}: tags are outside the "
                    "accepted subset",
                    line=number,
                )


def safe_load_subset(text: str) -> dict[str, Any]:
    """Parse the library's frontmatter subset into a dict.

    Supports block mappings, block sequences, mappings inside sequence items, block scalars
    (`>` folded and `|` literal, as used by every skill's `description`), plain and quoted
    multi-line scalars, flow lists of scalars, and comments.

    Tabs are rejected outright: in YAML a tab is never valid indentation, and accepting one
    produces a structure that looks plausible and is wrong. Block scalars are supported
    because the library uses `description: >` throughout — rejecting them would make this
    stdlib parser unusable against the real corpus.
    """
    lines = text.splitlines()
    _assert_subset_safe(text)
    parsed, _ = _parse_block(lines, 0, 0)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise FrontmatterError(
            f"frontmatter must be a mapping, got a {type(parsed).__name__}"
        )
    return parsed
def _read_block_scalar(lines: list[str], start: int, key_indent: int, *, folded: bool
                       ) -> tuple[str, int]:
    """Read a `>`/`|` block scalar, returning ``(text, next_index)``.

    The block ends at the first non-empty line indented at or below the key. A folded (`>`)
    scalar joins wrapped lines with spaces and turns blank lines into newlines; a literal
    (`|`) keeps newlines. Getting this wrong would corrupt every skill description, and
    every skill in the library uses `description: >`, so both forms are handled exactly
    rather than approximated.
    """
    collected: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            collected.append("")
            index += 1
            continue
        if _indent_of(line) <= key_indent:
            break
        collected.append(line.strip())
        index += 1

    # Trailing blank lines are separators, not content.
    while collected and collected[-1] == "":
        collected.pop()

    if not folded:
        return "\n".join(collected), index

    parts: list[str] = []
    for text in collected:
        if text == "":
            parts.append("\n")
            continue
        if parts and not parts[-1].endswith(("\n", " ")):
            parts.append(" ")
        parts.append(text)
    return "".join(parts), index


def _assign(root: dict[str, Any], stack: list[tuple[int, Any]], key: str,
            value: Any, indent: int) -> None:
    """Assign a key at the right nesting level, using the indentation stack."""
    while len(stack) > 1 and indent <= stack[-1][0]:
        stack.pop()
    target = stack[-1][1]
    if isinstance(target, dict):
        target[key] = value
    else:  # pragma: no cover - defensive; the stack only ever holds dicts here
        raise FrontmatterError(f"cannot assign {key!r} into a {type(target).__name__}")


def _assign_nested(root: dict[str, Any], key: str, value: Any, indent: int) -> None:
    """Assign a nested mapping under a key."""
    root.setdefault(key, {})
    if isinstance(root[key], dict):
        root[key] = value


def _try_pyyaml(text: str) -> dict[str, Any] | None:
    """Attempt a PyYAML `safe_load`, returning None when PyYAML is unavailable.

    `safe_load` only: frontmatter comes from a third-party repository and must never be
    able to instantiate arbitrary Python objects.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - any YAML failure falls back to the strict parser
        return None
    return data if isinstance(data, dict) else None


def _normalise(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise parsed frontmatter into plain Python containers.

    PyYAML returns tuples for some flow lists and `None` for empty blocks; the strict
    parser returns lists. Normalising here means callers never have to care which parser
    ran, which is what keeps the two paths interchangeable.
    """
    normalised: dict[str, Any] = {}
    for key, value in data.items():
        normalised[str(key)] = _normalise_value(value)
    return normalised


def _normalise_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalise_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise_value(v) for v in value]
    # A YAML date parses to `datetime.date` under PyYAML and to a string under the strict
    # parser. Normalising to the ISO string makes the two paths interchangeable, which is
    # the whole point of having a fallback.
    if hasattr(value, "isoformat") and not isinstance(value, (str, bytes)):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            return value
    if isinstance(value, str):
        # Folding rules differ subtly between parsers at a scalar's trailing line break —
        # PyYAML keeps it, a hand-written folder may not. A trailing newline on a metadata
        # field is never semantic (it never separates two values), so normalising it here
        # makes the two parsers produce identical output rather than merely similar output.
        # Internal newlines are preserved.
        return value.rstrip("\n")
    return value


def parse_frontmatter(text: str, *, prefer_pyyaml: bool = True) -> tuple[dict[str, Any], str]:
    """Parse a SKILL.md document into ``(frontmatter, body)``.

    The strict subset parser runs first when PyYAML is unavailable; when PyYAML is present
    it is tried first and its result is accepted only if it produces a mapping. Either way
    the returned shape is identical, so a caller cannot tell which path ran.

    Raises
    ------
    FrontmatterError
        On missing or unterminated frontmatter, or content outside the accepted subset.
    """
    front_text, body = split_document(text)
    if not front_text.strip():
        raise FrontmatterError("frontmatter block is empty")

    # Validate the subset before either parser runs, so a construct PyYAML would silently
    # resolve cannot make the two paths disagree.
    _assert_subset_safe(front_text)

    data: dict[str, Any] | None = None
    if prefer_pyyaml:
        data = _try_pyyaml(front_text)
    if data is None:
        data = safe_load_subset(front_text)
    return _normalise(data), body
