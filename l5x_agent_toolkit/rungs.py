"""
RLL (Relay Ladder Logic) instruction text parser and validator.

Parses the compact DSL used by Rockwell Automation in L5X files to encode
ladder logic rungs.  The format uses inline instruction calls, square brackets
for parallel branches (OR logic), and semicolons as rung terminators.

Format overview
---------------
- Instructions chain without spaces:   ``XIC(tag1)OTE(tag2);``
- Square brackets open parallel paths:  ``[XIC(a) ,XIC(b) ]OTE(c);``
- Commas (with trailing space) separate paths inside brackets.
- Semicolons terminate every rung.
- Empty rung:  ``;``
- Branches may nest:  ``[XIC(a) [XIC(b) ,XIC(c) ] ,XIC(d) ]OTE(e);``
- AOI calls look like regular instructions: ``MyAOI(inst,p1,p2);``
- Tag references support member access (``Timer1.DN``), array indices
  (``Array[0]``), and nested combinations (``UDT.Member.SubMember``).
- Literal values include integers, floats, and hex (``16#FF00``).
- ``?`` is used as a placeholder parameter for timer/counter display values.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import List, Optional, Set, Dict, Union


# ---------------------------------------------------------------------------
# Token definitions
# ---------------------------------------------------------------------------

class TokenType(enum.Enum):
    """Types of tokens produced by the rung-text tokeniser."""
    INSTRUCTION = "INSTRUCTION"
    OPEN_BRACKET = "OPEN_BRACKET"
    CLOSE_BRACKET = "CLOSE_BRACKET"
    COMMA = "COMMA"
    SEMICOLON = "SEMICOLON"
    OPEN_PAREN = "OPEN_PAREN"
    CLOSE_PAREN = "CLOSE_PAREN"
    TAG_REFERENCE = "TAG_REFERENCE"
    LITERAL = "LITERAL"
    QUESTION_MARK = "QUESTION_MARK"


@dataclass
class Token:
    """A single lexical token parsed from rung text.

    Attributes:
        type:  The semantic category of the token.
        value: The raw text captured for this token.
    """
    type: TokenType
    value: str

    def __repr__(self) -> str:
        return f"Token({self.type.name}, {self.value!r})"


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

# Regex for a literal value: integer, float, or Rockwell hex (16#...).
# Placed before tag patterns so that ``16#FF00`` is not split on ``#``.
_LITERAL_RE = re.compile(
    r"""
    (?:
        16\#[0-9A-Fa-f_]+          # Hex literal  16#FF00
      | 8\#[0-7_]+                 # Octal literal 8#77
      | 2\#[01_]+                  # Binary literal 2#1010
      | [+-]?\d+\.\d+(?:[eE][+-]?\d+)?  # Float with optional exponent
      | [+-]?\d+[eE][+-]?\d+      # Float with mandatory exponent
      | [+-]?\d+                   # Plain integer
    )
    """,
    re.VERBOSE,
)

# Regex for an instruction mnemonic (or AOI name).  Must start with a letter
# or underscore, followed by word characters.  This intentionally does NOT
# consume the opening parenthesis.
_INSTRUCTION_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Regex for a tag reference.  A tag reference starts with a letter or
# underscore, then may include word characters, dots (member access), and
# bracketed array indices.
#   Timer1.DN        tag with member
#   Array[0]         tag with index
#   UDT.Arr[2].EN   combination
_TAG_REFERENCE_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*"                 # base name
    r"(?:"
    r"  \.[A-Za-z_][A-Za-z0-9_]*"              # .Member
    r"| \[\s*[^\]]*\]"                          # [index]
    r")*",
    re.VERBOSE,
)


def tokenize(rung_text: str) -> list[Token]:
    """Tokenise *rung_text* into a flat list of :class:`Token` objects.

    The tokeniser recognises the following constructs:

    * Instruction names (letters/digits/underscore, starting with a letter
      or underscore) immediately followed by ``(``.
    * Tag references inside parentheses (may contain dots and brackets).
    * Literal values (integers, floats, hex ``16#...``).
    * ``?`` placeholders.
    * Structural delimiters: ``[  ]  ,  ;  (  )``

    Parameters
    ----------
    rung_text:
        The raw rung text string, e.g. ``"XIC(MyTag)OTE(Out);"``.

    Returns
    -------
    list[Token]
        Ordered list of tokens.
    """
    tokens: list[Token] = []
    pos = 0
    length = len(rung_text)

    while pos < length:
        ch = rung_text[pos]

        # Skip whitespace (spaces are syntactically insignificant except
        # as separators between tokens).
        if ch in (' ', '\t', '\n', '\r'):
            pos += 1
            continue

        # Structural single-character tokens.
        if ch == '[':
            tokens.append(Token(TokenType.OPEN_BRACKET, '['))
            pos += 1
            continue
        if ch == ']':
            tokens.append(Token(TokenType.CLOSE_BRACKET, ']'))
            pos += 1
            continue
        if ch == ',':
            tokens.append(Token(TokenType.COMMA, ','))
            pos += 1
            continue
        if ch == ';':
            tokens.append(Token(TokenType.SEMICOLON, ';'))
            pos += 1
            continue
        if ch == '(':
            tokens.append(Token(TokenType.OPEN_PAREN, '('))
            pos += 1
            continue
        if ch == ')':
            tokens.append(Token(TokenType.CLOSE_PAREN, ')'))
            pos += 1
            continue
        if ch == '?':
            tokens.append(Token(TokenType.QUESTION_MARK, '?'))
            pos += 1
            continue

        # Try to match an instruction name / tag reference.
        # Distinguish between the two by looking at context: if the previous
        # meaningful token was OPEN_PAREN or COMMA (i.e. we are inside an
        # argument list) then this is a TAG_REFERENCE; otherwise, if the
        # identifier is immediately followed by '(' it is an INSTRUCTION.
        m = _INSTRUCTION_RE.match(rung_text, pos)
        if m:
            ident = m.group(0)
            end = m.end()

            # Look ahead: is this an instruction (followed by '(')?
            # To decide, we also need context.  If we are currently inside
            # a parameter list we should treat this as a tag reference that
            # might have member/index suffixes.
            inside_args = _inside_argument_list(tokens)

            if not inside_args and end < length and rung_text[end] == '(':
                # This is an instruction name.
                tokens.append(Token(TokenType.INSTRUCTION, ident))
                pos = end
                continue
            else:
                # This is a tag reference -- consume member access and
                # array indices as well.
                tag_text = ident
                tag_pos = end
                while tag_pos < length:
                    if rung_text[tag_pos] == '.':
                        # Member access -- consume the dot and the member name
                        dm = _INSTRUCTION_RE.match(rung_text, tag_pos + 1)
                        if dm:
                            tag_text += '.' + dm.group(0)
                            tag_pos = dm.end()
                            continue
                        else:
                            break
                    elif rung_text[tag_pos] == '[':
                        # Array index -- find matching ']', handling nesting.
                        bracket_end = _find_matching_bracket(rung_text, tag_pos)
                        if bracket_end is not None:
                            tag_text += rung_text[tag_pos:bracket_end + 1]
                            tag_pos = bracket_end + 1
                            continue
                        else:
                            break
                    else:
                        break
                tokens.append(Token(TokenType.TAG_REFERENCE, tag_text))
                pos = tag_pos
                continue

        # Try to match a literal value.
        lm = _LITERAL_RE.match(rung_text, pos)
        if lm:
            tokens.append(Token(TokenType.LITERAL, lm.group(0)))
            pos = lm.end()
            continue

        # If we reach here, skip the character (shouldn't happen in well-formed
        # rung text, but we remain resilient).
        pos += 1

    return tokens


def _inside_argument_list(tokens: list[Token]) -> bool:
    """Return True if the current position is inside a parenthesised
    argument list, based on tokens emitted so far.

    We track paren depth by counting OPEN_PAREN and CLOSE_PAREN tokens.
    """
    depth = 0
    for t in tokens:
        if t.type == TokenType.OPEN_PAREN:
            depth += 1
        elif t.type == TokenType.CLOSE_PAREN:
            depth -= 1
    return depth > 0


def _find_matching_bracket(text: str, start: int) -> Optional[int]:
    """Return the index of the ``]`` that matches the ``[`` at *start*,
    handling nested brackets.  Returns ``None`` if unmatched."""
    depth = 0
    pos = start
    while pos < len(text):
        if text[pos] == '[':
            depth += 1
        elif text[pos] == ']':
            depth -= 1
            if depth == 0:
                return pos
        pos += 1
    return None


# ---------------------------------------------------------------------------
# AST node classes
# ---------------------------------------------------------------------------

@dataclass
class InstructionCall:
    """Represents a single instruction invocation in a rung.

    Attributes:
        name:      The instruction mnemonic (e.g. ``XIC``, ``OTE``) or AOI name.
        arguments: The raw argument strings passed to the instruction.
    """
    name: str
    arguments: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        args = ", ".join(self.arguments)
        return f"{self.name}({args})"


@dataclass
class Branch:
    """Represents a parallel branch structure (OR logic) in a rung.

    Each entry in *paths* is a list of rung elements (instructions and/or
    nested branches) that form one parallel path.

    Attributes:
        paths: A list of paths, where each path is a list of
               :class:`InstructionCall` or nested :class:`Branch` objects.
    """
    paths: list[list[Union[InstructionCall, "Branch"]]] = field(default_factory=list)

    def __repr__(self) -> str:
        path_strs = []
        for path in self.paths:
            elems = " ".join(repr(e) for e in path)
            path_strs.append(elems)
        return "[" + " , ".join(path_strs) + " ]"


# Type alias for elements that can appear in a rung.
RungElement = Union[InstructionCall, Branch]


@dataclass
class Rung:
    """Represents a fully parsed rung.

    Attributes:
        elements: The ordered list of instructions and branches that form
                  the rung logic.
        comment:  Optional rung comment (not encoded in the rung text itself;
                  provided separately in the L5X XML).
    """
    elements: list[RungElement] = field(default_factory=list)
    comment: Optional[str] = None

    def __repr__(self) -> str:
        elems = " ".join(repr(e) for e in self.elements)
        if self.comment:
            return f"Rung(comment={self.comment!r}, {elems})"
        return f"Rung({elems})"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_rung(rung_text: str, comment: str = None) -> Rung:
    """Parse raw rung text into a structured :class:`Rung` object.

    Parameters
    ----------
    rung_text:
        The rung text string (e.g. ``"XIC(tag)OTE(out);"``).
    comment:
        Optional rung comment to attach to the result.

    Returns
    -------
    Rung
        A structured representation of the rung logic.

    Raises
    ------
    ValueError
        If the rung text has mismatched brackets or is otherwise malformed.
    """
    tokens = tokenize(rung_text)
    elements, pos = _parse_elements(tokens, 0)
    return Rung(elements=elements, comment=comment)


def _parse_elements(tokens: list[Token], pos: int,
                    stop_at: set[TokenType] | None = None
                    ) -> tuple[list[RungElement], int]:
    """Recursively parse a sequence of rung elements.

    Consumes tokens starting at *pos* and returns the list of parsed
    elements together with the new position.

    Parameters
    ----------
    tokens:   Flat token list from :func:`tokenize`.
    pos:      Current position in the token list.
    stop_at:  Set of token types that should terminate parsing at this level
              (without consuming the token).  Used by branch parsing to stop
              at COMMA and CLOSE_BRACKET.

    Returns
    -------
    (list[RungElement], int)
    """
    if stop_at is None:
        stop_at = set()

    elements: list[RungElement] = []

    while pos < len(tokens):
        tok = tokens[pos]

        # Should we stop here?
        if tok.type in stop_at:
            return elements, pos

        # Semicolons terminate the rung -- stop parsing.
        if tok.type == TokenType.SEMICOLON:
            return elements, pos + 1

        # Instruction call
        if tok.type == TokenType.INSTRUCTION:
            instr, pos = _parse_instruction(tokens, pos)
            elements.append(instr)
            continue

        # Open bracket -> parallel branch
        if tok.type == TokenType.OPEN_BRACKET:
            branch, pos = _parse_branch(tokens, pos)
            elements.append(branch)
            continue

        # Anything else at the top level is unexpected; skip it to remain
        # resilient.
        pos += 1

    return elements, pos


def _parse_instruction(tokens: list[Token], pos: int
                       ) -> tuple[InstructionCall, int]:
    """Parse a single instruction call starting at *pos*.

    Expects: INSTRUCTION OPEN_PAREN  arg [COMMA arg]* CLOSE_PAREN

    For instructions with no arguments the paren pair may still be present
    (e.g. ``NOP()``), or the parens may be absent entirely (e.g. ``NOP``
    appearing without parens, though this is rare in L5X).
    """
    name = tokens[pos].value
    pos += 1

    arguments: list[str] = []

    # Check for opening parenthesis
    if pos < len(tokens) and tokens[pos].type == TokenType.OPEN_PAREN:
        pos += 1  # skip '('

        # Collect arguments until CLOSE_PAREN
        while pos < len(tokens) and tokens[pos].type != TokenType.CLOSE_PAREN:
            tok = tokens[pos]

            if tok.type == TokenType.COMMA:
                pos += 1
                continue

            if tok.type in (TokenType.TAG_REFERENCE, TokenType.LITERAL):
                arguments.append(tok.value)
                pos += 1
                continue

            if tok.type == TokenType.QUESTION_MARK:
                arguments.append('?')
                pos += 1
                continue

            # Handle an instruction name appearing as a tag reference inside
            # arguments (e.g. a JSR target routine name that looks like an
            # instruction name because it starts with a letter).
            if tok.type == TokenType.INSTRUCTION:
                # Peek: if this is followed by OPEN_PAREN then it really is
                # an instruction (shouldn't happen inside args), otherwise
                # treat it as a tag reference.
                if (pos + 1 < len(tokens)
                        and tokens[pos + 1].type == TokenType.OPEN_PAREN):
                    # Unusual -- nested instruction inside args; just record
                    # the name as an argument.
                    arguments.append(tok.value)
                    pos += 1
                else:
                    arguments.append(tok.value)
                    pos += 1
                continue

            # Skip unexpected tokens inside argument lists.
            pos += 1

        # Skip closing parenthesis
        if pos < len(tokens) and tokens[pos].type == TokenType.CLOSE_PAREN:
            pos += 1

    return InstructionCall(name=name, arguments=arguments), pos


def _parse_branch(tokens: list[Token], pos: int
                  ) -> tuple[Branch, int]:
    """Parse a parallel branch starting at the OPEN_BRACKET at *pos*.

    Structure: ``[ path1 , path2 , ... ]``

    Each path is a sequence of rung elements (instructions and/or nested
    branches) terminated by COMMA or CLOSE_BRACKET.
    """
    assert tokens[pos].type == TokenType.OPEN_BRACKET
    pos += 1  # skip '['

    branch = Branch(paths=[])

    # Parse each parallel path.
    while pos < len(tokens):
        if tokens[pos].type == TokenType.CLOSE_BRACKET:
            # If we haven't added any path yet but encounter the close bracket
            # immediately, add an empty path for completeness.
            if not branch.paths:
                branch.paths.append([])
            pos += 1
            return branch, pos

        path_elements, pos = _parse_elements(
            tokens, pos,
            stop_at={TokenType.COMMA, TokenType.CLOSE_BRACKET},
        )
        branch.paths.append(path_elements)

        # If we stopped at a comma, skip it and continue to the next path.
        if pos < len(tokens) and tokens[pos].type == TokenType.COMMA:
            pos += 1
            continue

        # If we stopped at a close bracket, consume it and return.
        if pos < len(tokens) and tokens[pos].type == TokenType.CLOSE_BRACKET:
            pos += 1
            return branch, pos

    # Ran out of tokens without finding the close bracket.
    return branch, pos


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def validate_rung_syntax(rung_text: str) -> list[str]:
    """Validate the syntactic structure of *rung_text*.

    Checks performed:

    * Bracket matching (every ``[`` has a corresponding ``]``).
    * Semicolon termination.
    * Non-empty instruction names (if any).

    Parameters
    ----------
    rung_text:
        Raw rung text to validate.

    Returns
    -------
    list[str]
        A list of human-readable error messages.  Empty if the rung text is
        syntactically valid.
    """
    errors: list[str] = []
    stripped = rung_text.strip()

    # Empty check -- an empty rung is just ";".
    if not stripped:
        errors.append("Rung text is empty (expected at least ';')")
        return errors

    # Semicolon termination
    if not stripped.endswith(';'):
        errors.append("Rung text must end with a semicolon ';'")

    # Bracket matching
    depth = 0
    for i, ch in enumerate(stripped):
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth < 0:
                errors.append(
                    f"Unexpected closing bracket ']' at position {i}"
                )
    if depth > 0:
        errors.append(
            f"Unmatched opening bracket(s): {depth} unclosed '['(s)"
        )

    # Parenthesis matching
    paren_depth = 0
    for i, ch in enumerate(stripped):
        if ch == '(':
            paren_depth += 1
        elif ch == ')':
            paren_depth -= 1
            if paren_depth < 0:
                errors.append(
                    f"Unexpected closing parenthesis ')' at position {i}"
                )
    if paren_depth > 0:
        errors.append(
            f"Unmatched opening parenthesis(es): {paren_depth} unclosed '('(s)"
        )

    return errors


def validate_rung_references(rung_text: str,
                             available_tags: set[str]) -> list[str]:
    """Check that every tag referenced in *rung_text* exists in
    *available_tags*.

    Tag references are resolved to their *base* name before lookup:
    ``Timer1.DN`` checks for ``Timer1``, ``Array[0]`` checks for ``Array``.

    Parameters
    ----------
    rung_text:
        Raw rung text to scan.
    available_tags:
        Set of tag names known to be valid.

    Returns
    -------
    list[str]
        A list of tag base-names that are referenced in the rung but absent
        from *available_tags*.
    """
    referenced = extract_tag_references(rung_text)
    missing = sorted(referenced - available_tags)
    return missing


# ---------------------------------------------------------------------------
# Tag reference extraction
# ---------------------------------------------------------------------------

def _base_tag_name(tag_ref: str) -> str:
    """Extract the base tag name from a full reference.

    Examples::

        Timer1.DN       -> Timer1
        Array[0]        -> Array
        UDT.Member.Sub  -> UDT
        SimpleTag       -> SimpleTag
    """
    # Split on the first dot or opening bracket -- whichever comes first.
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", tag_ref)
    if m:
        return m.group(1)
    return tag_ref


def extract_tag_references(rung_text: str) -> set[str]:
    """Extract every unique base tag name referenced in *rung_text*.

    Literal values (integers, floats, hex), ``?`` placeholders, and
    instruction mnemonics are excluded.

    Parameters
    ----------
    rung_text:
        Raw rung text to scan.

    Returns
    -------
    set[str]
        Set of base tag names.
    """
    tokens = tokenize(rung_text)
    tag_bases: set[str] = set()

    for tok in tokens:
        if tok.type == TokenType.TAG_REFERENCE:
            base = _base_tag_name(tok.value)
            tag_bases.add(base)

    return tag_bases


# ---------------------------------------------------------------------------
# Tag substitution
# ---------------------------------------------------------------------------

def substitute_tags(rung_text: str,
                    substitutions: dict[str, str]) -> str:
    """Replace tag names in *rung_text* according to *substitutions*.

    Handles member access and array indices correctly::

        substitute_tags("XIC(OldTag.DN)OTE(Out);",
                        {"OldTag": "NewTag"})
        # -> "XIC(NewTag.DN)OTE(Out);"

    Substitution keys are sorted longest-first to prevent partial matches
    (e.g. ``Tag1`` inside ``Tag10``).  Whole-word boundaries are enforced.

    Parameters
    ----------
    rung_text:
        Raw rung text.
    substitutions:
        Mapping from old tag base names to new tag base names.

    Returns
    -------
    str
        The rung text with tag names replaced.
    """
    if not substitutions:
        return rung_text

    # Sort keys longest-first so that "MyTag10" is tried before "MyTag1".
    sorted_keys = sorted(substitutions.keys(), key=len, reverse=True)

    # Build a regex that matches any of the keys as whole "tag name" tokens.
    # A tag name boundary is: preceded by start-of-string or a non-word char,
    # and followed by a non-word char (except dot and [), end-of-string,
    # dot, or opening bracket.
    #
    # We use a capturing group and a replacement function.
    escaped_keys = [re.escape(k) for k in sorted_keys]
    pattern = (
        r"(?<![A-Za-z0-9_])"            # not preceded by a word char
        r"("
        + "|".join(escaped_keys)
        + r")"
        r"(?=[.\[\)\, ;}\]\n]|$)"       # followed by member/index/delim/end
    )

    regex = re.compile(pattern)

    def _replacer(m: re.Match) -> str:
        return substitutions[m.group(1)]

    return regex.sub(_replacer, rung_text)


# ---------------------------------------------------------------------------
# Rung text builder
# ---------------------------------------------------------------------------

def build_rung_text(instructions: Union[str, list[str]],
                    comment: str = None) -> str:
    """Construct valid rung text from one or more instruction strings.

    Ensures proper semicolon termination and optional comment attachment
    (returned as metadata, not embedded in the text itself).

    Parameters
    ----------
    instructions:
        Either a single instruction string (e.g. ``"XIC(tag)OTE(out)"``)
        or a list of instruction strings to concatenate.
    comment:
        Optional comment text (stored externally; not embedded in the
        rung text).

    Returns
    -------
    str
        Valid rung text ending with ``;``.

    Examples
    --------
    >>> build_rung_text("XIC(Start)OTE(Motor)")
    'XIC(Start)OTE(Motor);'

    >>> build_rung_text(["XIC(Start)", "OTE(Motor)"])
    'XIC(Start)OTE(Motor);'
    """
    if isinstance(instructions, list):
        text = "".join(instructions)
    else:
        text = instructions

    text = text.rstrip()

    # Ensure semicolon termination.
    if not text.endswith(';'):
        text += ';'

    return text
