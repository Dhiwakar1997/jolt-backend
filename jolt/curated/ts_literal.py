"""Read a data literal out of a TypeScript source file (BE-09).

The v1 DSA library (`library.ts`) and decision tree (`decisionTree.ts`) are
plain object literals assigned to an exported const. This reads such a literal
without Node: objects (bare, quoted or numeric keys, trailing commas), arrays,
single/double-quoted strings, template strings without `${}`, numbers,
`true` / `false` / `null`, and comments. Anything else (a function call, a
spread, a variable reference, a shorthand property) raises `LiteralError`, so a
change in the source's shape fails loudly instead of importing wrong data.
"""

from __future__ import annotations

import re
from typing import Any


class LiteralError(ValueError):
    pass


_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "`": "`",
    "\n": "",
}
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


class _Parser:
    def __init__(self, text: str, pos: int) -> None:
        self.s = text
        self.i = pos

    def error(self, message: str) -> LiteralError:
        line = self.s.count("\n", 0, self.i) + 1
        return LiteralError(f"{message} (line {line})")

    def skip(self) -> None:
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c.isspace():
                self.i += 1
            elif s.startswith("//", self.i):
                end = s.find("\n", self.i)
                self.i = len(s) if end < 0 else end + 1
            elif s.startswith("/*", self.i):
                end = s.find("*/", self.i + 2)
                if end < 0:
                    raise self.error("unterminated comment")
                self.i = end + 2
            else:
                return

    def peek(self) -> str:
        self.skip()
        return self.s[self.i] if self.i < len(self.s) else ""

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            raise self.error(f"expected {ch!r}, found {self.peek()!r}")
        self.i += 1

    def value(self) -> Any:
        c = self.peek()
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c in "'\"`":
            return self.string()
        m = _NUMBER.match(self.s, self.i)
        if m and (c.isdigit() or c in "-."):
            self.i = m.end()
            text = m.group(0)
            return float(text) if any(ch in text for ch in ".eE") else int(text)
        m = _IDENT.match(self.s, self.i)
        if m:
            word = m.group(0)
            if word in ("true", "false", "null"):
                self.i = m.end()
                return {"true": True, "false": False, "null": None}[word]
            raise self.error(f"not a data literal: {word!r}")
        raise self.error(f"unexpected {c!r}")

    def string(self) -> str:
        quote = self.s[self.i]
        self.i += 1
        out: list[str] = []
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c == quote:
                self.i += 1
                return "".join(out)
            if c == "\\":
                nxt = s[self.i + 1] if self.i + 1 < len(s) else ""
                if nxt == "u":
                    if s.startswith("{", self.i + 2):
                        end = s.index("}", self.i + 3)
                        out.append(chr(int(s[self.i + 3 : end], 16)))
                        self.i = end + 1
                    else:
                        out.append(chr(int(s[self.i + 2 : self.i + 6], 16)))
                        self.i += 6
                    continue
                if nxt == "x":
                    out.append(chr(int(s[self.i + 2 : self.i + 4], 16)))
                    self.i += 4
                    continue
                out.append(_ESCAPES.get(nxt, nxt))
                self.i += 2
                continue
            if quote == "`" and s.startswith("${", self.i):
                raise self.error("template interpolation is not a data literal")
            if c == "\n" and quote != "`":
                raise self.error("unterminated string")
            out.append(c)
            self.i += 1
        raise self.error("unterminated string")

    def key(self) -> str:
        c = self.peek()
        if c in "'\"":
            return self.string()
        m = _IDENT.match(self.s, self.i) or _NUMBER.match(self.s, self.i)
        if not m:
            raise self.error(f"bad object key at {c!r}")
        self.i = m.end()
        return m.group(0)

    def obj(self) -> dict:
        self.expect("{")
        out: dict[str, Any] = {}
        while True:
            if self.peek() == "}":
                self.i += 1
                return out
            if self.s.startswith("...", self.i):
                raise self.error("spread is not a data literal")
            k = self.key()
            if self.peek() != ":":
                raise self.error(f"shorthand or computed property {k!r} is not a data literal")
            self.i += 1
            out[k] = self.value()
            if self.peek() == ",":
                self.i += 1
            elif self.peek() != "}":
                raise self.error(f"expected ',' or '}}' after {k!r}")

    def arr(self) -> list:
        self.expect("[")
        out: list[Any] = []
        while True:
            if self.peek() == "]":
                self.i += 1
                return out
            out.append(self.value())
            if self.peek() == ",":
                self.i += 1
            elif self.peek() != "]":
                raise self.error("expected ',' or ']'")


def read_export(source: str, name: str) -> Any:
    """The literal assigned to `export const <name>` in a TS/JS source."""
    m = re.search(rf"export\s+const\s+{re.escape(name)}\b[^=]*=", source)
    if not m:
        raise LiteralError(f"no `export const {name}` found")
    parser = _Parser(source, m.end())
    value = parser.value()
    parser.skip()
    rest = source[parser.i :]
    # The literal must end the statement (`;`, `as const`, or a new line).
    if rest and not rest.startswith((";", "as const")) and source[parser.i - 1] != "\n":
        raise parser.error(f"`{name}` is not a plain data literal")
    return value
