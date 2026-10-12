"""Reading variables.tf and editing tfvars.

No HCL parser is pulled in. The files involved are `key = value` one-liners
(plus one list), and the write path must preserve comments and alignment
byte-for-byte, which a parse-and-regenerate round trip cannot do.
"""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

# Variables that get a default in variables.tf but still belong in the Required
# tab: bedrock_model_id has a default but picking the model is a human decision.
ALWAYS_REQUIRED = frozenset({"bedrock_model_id"})


@dataclass(frozen=True)
class TfVariable:
    name: str
    type: str
    description: str
    sensitive: bool
    has_default: bool
    default_literal: str | None

    @property
    def required(self) -> bool:
        """Required = Terraform demands a value, plus two explicit exceptions."""
        return (not self.has_default) or self.name in ALWAYS_REQUIRED


def _blocks(text: str):
    """Yield (name, body) for each top-level `variable "x" { ... }`.

    Brace counting rather than a regex for the body: descriptions contain
    braces and nested blocks (validation) would break a lazy match.
    """
    for m in re.finditer(r'variable\s+"([a-z_0-9]+)"\s*\{', text):
        i, depth = m.end(), 1
        while depth and i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        yield m.group(1), text[m.end() : i - 1]


def _description(body: str) -> str:
    heredoc = re.search(r"description\s*=\s*<<-?([A-Z]+)\n(.*?)\n\s*\1", body, re.S)
    if heredoc:
        return textwrap.dedent(heredoc.group(2)).strip()
    quoted = re.search(r'description\s*=\s*"((?:[^"\\]|\\.)*)"', body)
    if quoted:
        return quoted.group(1).replace('\\"', '"')
    return ""


def _default_literal(body: str) -> str | None:
    """The default as one HCL literal, even when it spans lines.

    A first-line-only match turned a three-item list default into `[`, which the
    form showed as empty and saved as `[]` — silently dropping the stack's
    default model list on a real run. Brackets are balanced here instead.
    """
    m = re.search(r"^\s*default\s*=\s*(.+?)\s*$", body, re.M)
    if not m:
        return None
    first = m.group(1)
    if first[0] not in "[{" or _balanced(first):
        return first
    rest = body[m.end():]
    collected = first
    for line in rest.splitlines():
        collected += " " + line.strip()
        if _balanced(collected):
            break
    # Normalise `[ "a", "b", ]` to `["a", "b"]`: trailing commas are legal HCL
    # but the form shows this text verbatim.
    collected = re.sub(r",\s*([\]}])", r"\1", collected)
    collected = re.sub(r"([\[{])\s+", r"\1", collected)
    return re.sub(r"\s+([\]}])", r"\1", collected)


def _balanced(text: str) -> bool:
    depth = 0
    in_str = False
    for ch in text:
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
    return depth == 0 and not in_str


def parse_variables(text: str) -> tuple[TfVariable, ...]:
    out = []
    for name, body in _blocks(text):
        type_m = re.search(r"^\s*type\s*=\s*(.+?)\s*$", body, re.M)
        default = _default_literal(body)
        out.append(
            TfVariable(
                name=name,
                type=type_m.group(1) if type_m else "string",
                description=_description(body),
                sensitive=bool(re.search(r"^\s*sensitive\s*=\s*true", body, re.M)),
                has_default=default is not None,
                default_literal=default,
            )
        )
    return tuple(out)


def read_variables(path: Path) -> tuple[TfVariable, ...]:
    return parse_variables(path.read_text())


def render_value(value, type_: str) -> str:
    """Render a Python value as the HCL literal for `type_`."""
    if type_ == "bool":
        return "true" if value else "false"
    if type_ == "number":
        return str(value)
    if type_.startswith("list("):
        items = value if isinstance(value, (list, tuple)) else _split_list(str(value))
        return "[" + ", ".join(_quote(str(i)) for i in items) + "]"
    if type_.startswith("map("):
        return _render_map(value)
    return _quote(str(value))


def _render_map(value) -> str:
    """map(string): accept an HCL literal or `key = value` pairs.

    Falling through to _quote wrote `runtime_mcp_servers = "{}"`, which
    terraform rejects ("map of string required, but have string") — the first
    apply failed on a real run before any resource was touched. The form shows
    the default literal `{}`, so a literal typed or left as-is is written
    verbatim; `name = arn, other = arn2` is rendered as a map for convenience.
    """
    if isinstance(value, dict):
        pairs = list(value.items())
    else:
        raw = str(value).strip()
        if not raw or raw == "{}":
            return "{}"
        if raw.startswith("{"):
            return raw
        pairs = [tuple(p.split("=", 1)) for p in _split_list(raw) if "=" in p]
    if not pairs:
        return "{}"
    body = ", ".join(f"{_quote(str(k).strip())} = {_quote(str(v).strip())}" for k, v in pairs)
    return "{ " + body + " }"


def _split_list(raw: str) -> list[str]:
    """Accept newline- or comma-separated input from a multi-line widget."""
    return [p.strip() for p in re.split(r"[\n,]", raw) if p.strip()]


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _assignment_re(key: str) -> re.Pattern[str]:
    """Match `key = ...` at the start of a line, ignoring commented-out ones.

    The leading group captures indentation and the second captures the padding
    before `=` so the file's alignment survives the edit. A `#` anywhere before
    the key means this is a comment, not an assignment.
    """
    return re.compile(rf"^([ \t]*){re.escape(key)}([ \t]*)=.*$", re.M)


def set_var(text: str, key: str, rendered: str) -> str:
    """Replace `key`'s assignment, or append it if absent."""
    pattern = _assignment_re(key)
    if pattern.search(text):
        return pattern.sub(
            lambda m: f"{m.group(1)}{key}{m.group(2)}= {rendered}", text, count=1
        )
    separator = "" if not text or text.endswith("\n") else "\n"
    return f"{text}{separator}{key} = {rendered}\n"


def read_values(text: str) -> dict[str, str]:
    """Return {key: raw literal} for uncommented assignments."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^[ \t]*([a-z_0-9]+)[ \t]*=[ \t]*(.+?)[ \t]*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out
