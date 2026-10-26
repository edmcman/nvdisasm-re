"""Publish unchanged literate K source and collect rule provenance."""
from dataclasses import dataclass
import re


STRINGS = re.compile(r'"(?:\\.|[^"\\])*"')
BLOCKS = re.compile(r"^```k[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
START = re.compile(r"^\s*rule\s+")
DECLARATION = re.compile(r"^\s*(?:rule|syntax|configuration|module|endmodule|imports)\b")


def outside_strings(text):
    """Mask quoted contents while retaining character positions."""
    return STRINGS.sub(lambda match: " " * len(match[0]), text)


@dataclass(frozen=True)
class Rule:
    source: str
    line: int
    text: str


def publication(source):
    """Keep code blocks verbatim; record exact rule text and source locations."""
    text = source.read_text()
    rules = []
    for block in BLOCKS.finditer(text):
        lines = block[1].splitlines(keepends=True)
        base = text.count("\n", 0, block.start(1)) + 1
        index = 0
        while index < len(lines):
            if not START.match(lines[index]):
                index += 1
                continue
            start = index
            index += 1
            while index < len(lines):
                if (not lines[index].strip() or DECLARATION.match(lines[index])
                        or lines[index].lstrip().startswith("//")):
                    break
                index += 1
            rule = Rule(source.name, base + start, "".join(lines[start:index]).rstrip())
            if "=>" not in outside_strings(rule.text):
                raise ValueError(f"expected K rewrite at {rule.source}:{rule.line}")
            rules.append(rule)
    return text, rules
