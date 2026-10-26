"""Typeset K rule text without interpreting or composing its rewrites."""
from dataclasses import dataclass
import re


STRINGS = re.compile(r'"(?:\\.|[^"\\])*"')
BLOCKS = re.compile(r"^```k[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
START = re.compile(r"^\s*rule\s+")
DECLARATION = re.compile(r"^\s*(?:rule|syntax|configuration|module|endmodule|imports)\b")


def outside_strings(text):
    """Mask quoted contents while retaining character positions."""
    return STRINGS.sub(lambda match: " " * len(match[0]), text)


def textext(text):
    """Escape literal K text, changing only unquoted rewrite tokens."""
    escapes = {
        "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}",
        "$": r"\$", "&": r"\&", "#": r"\#", "%": r"\%",
        "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
        "<": r"\textless{}", ">": r"\textgreater{}",
    }
    positions = {match.start() for match in re.finditer("=>", outside_strings(text))}
    parts, index = [], 0
    while index < len(text):
        if index in positions:
            parts.append(r"\ensuremath{\Longrightarrow}")
            index += 2
        else:
            parts.append(escapes.get(text[index], text[index]))
            index += 1
    return "".join(parts)


def formula(text):
    """Use natural math widths; wrap only at whitespace outside string literals."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        while len(line) > 100:
            strings = [match.span() for match in STRINGS.finditer(line)]
            spaces = [match for match in re.finditer(r"\s+", line)
                      if 0 < match.start() <= 100
                      and not any(start <= match.start() < end for start, end in strings)]
            if not spaces:
                break
            space = spaces[-1]
            rows.append(line[:space.start()])
            line = line[space.end():]
        if line:
            rows.append(line)
    if not rows:
        return r"\vphantom{\text{\footnotesize K}}"
    return (r"\begin{gathered}"
            + r"\\".join(r"\text{\ttfamily\footnotesize " + textext(row) + "}" for row in rows)
            + r"\end{gathered}")


@dataclass(frozen=True)
class Rule:
    source: str
    line: int
    text: str

    def parts(self):
        """Separate premise and attributes, preserving their literal K spelling."""
        body = START.sub("", self.text, count=1).strip()
        visible = outside_strings(body)
        attribute = re.search(r"\s+(\[[^\]\n]+\])\s*$", visible)
        attributes = body[attribute.start(1):] if attribute else ""
        if attribute:
            body = body[:attribute.start(1)].rstrip()
        condition = re.search(r"\brequires\b", outside_strings(body))
        if condition:
            return body[:condition.start()].rstrip(), body[condition.end():].strip(), attributes
        return body, "", attributes

    def latex(self):
        conclusion, premise, attributes = self.parts()
        label = self.source.replace("_", r"\_")
        # Keep tokens intact while putting configuration cells on separate lines.
        for match in reversed(list(re.finditer(r"<[A-Za-z]\w*>", outside_strings(conclusion)))):
            if match.start():
                conclusion = conclusion[:match.start()] + "\n" + conclusion[match.start():]
        lines = [r"\[\begin{gathered}",
                 r"\frac{" + formula(premise) + "}{" + formula(conclusion) + "}"]
        metadata = rf"\text{{\scriptsize\color{{gray}}{label}:{self.line}}}"
        if attributes:
            metadata += r"\qquad\text{\ttfamily\scriptsize " + textext(attributes) + "}"
        lines += [r"\\[2pt]" + metadata, r"\end{gathered}\]"]
        return "\n".join(lines)


def publication(source):
    """Render the line-oriented handwritten sources, retaining non-rule declarations."""
    text = source.read_text()
    rules = []

    def block(match):
        lines = match[1].splitlines(keepends=True)
        base = text.count("\n", 0, match.start(1)) + 1
        pieces, declarations = [], []

        def flush():
            if "".join(declarations).strip():
                pieces.append("```k\n" + "".join(declarations).rstrip() + "\n```")
            declarations.clear()

        index = 0
        while index < len(lines):
            if not START.match(lines[index]):
                declarations.append(lines[index])
                index += 1
                continue
            flush()
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
            pieces.append("```{=latex}\n" + rule.latex() + "\n```")
        flush()
        return "\n\n".join(pieces)

    rendered = BLOCKS.sub(block, text)
    return rendered, rules
