"""Typeset source K rules using a bounded mathematical syntax presentation."""
from dataclasses import dataclass
import re

from sass.formal.k import notation


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


def formula(text, *, width=72, align="c", prefix="", suffix=""):
    """Render parsed terms, keeping continuations within their formula column."""
    if not text:
        return r"\vphantom{\text{\footnotesize K}}"
    term = notation.parse(text) if isinstance(text, str) else text
    rows = notation.rows(term, width)
    rows[0] = prefix + rows[0]
    rows[-1] += suffix
    return (r"\begin{array}[t]{@{}" + align + r"@{}}"
            + r"\\".join(rows)
            + r"\end{array}")


def cell(text):
    """Separate a cell's name and framing ellipses from its term or local rewrite."""
    visible = outside_strings(text)
    match = re.fullmatch(r"<([A-Za-z]\w*)>(.*)</\1>", visible, re.DOTALL)
    if not match:
        raise ValueError(f"unsupported cell syntax: {text!r}")
    body = text[match.start(2):match.end(2)].strip()
    before = body.startswith("...")
    after = body.endswith("...")
    if before:
        body = body[3:].lstrip()
    if after:
        body = body[:-3].rstrip()
    opening = r"\langle " + (r"\cdots\, " if before else "")
    closing = (r"\,\cdots " if after else "") + r"\rangle_{\mathrm{" + match[1] + "}}"
    return body, opening, closing


def rewrites(text):
    """Keep local rewrites inside mathematical cells; never compose transitions."""
    cells = list(re.finditer(r"<[A-Za-z]\w*>", outside_strings(text)))
    starts = [match.start() for match in cells] if cells and cells[0].start() == 0 else [0]
    blocks = [text[start:end].strip()
              for start, end in zip(starts, starts[1:] + [len(text)])]
    rows = []
    for block in blocks:
        opening, closing = "", ""
        if block.startswith("<"):
            block, opening, closing = cell(block)
        arrows = list(re.finditer("=>", outside_strings(block)))
        if not arrows:
            rows.append(r"\multicolumn{3}{c}{"
                        + formula(block, prefix=opening, suffix=closing) + "}")
            continue
        if len(arrows) != 1:
            raise ValueError("expected one local rewrite per cell or function rule")
        position = arrows[0].start()
        left, right = block[:position].strip(), block[position + 2:].strip()
        left, right = notation.parse(left), notation.parse(right)
        left_width = min(32, left.width())
        right_width = max(25, 72 - left_width - 8)
        local = (formula(left, width=32, align="l")
                 + r"&\Longrightarrow&"
                 + formula(right, width=right_width, align="l"))
        if opening:
            # Each cell has its own natural-width columns. Enclose the whole
            # rewrite so a multi-line term cannot outlive its closing bracket.
            local = (opening.replace(r"\langle", r"\left\langle")
                     + r"\begin{array}{@{}r@{\;}c@{\;}l@{}}" + local + r"\end{array}"
                     + closing.replace(r"\rangle", r"\right\rangle"))
            rows.append(r"\multicolumn{3}{c}{" + local + "}")
        else:
            rows.append(local)
    return r"\begin{array}{@{}r@{\;}c@{\;}l@{}}" + r"\\[2pt]".join(rows) + r"\end{array}"


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
        lines = [r"{\small\[\begin{gathered}",
                 r"\frac{" + formula(premise) + "}{" + rewrites(conclusion) + "}"]
        metadata = rf"\text{{\scriptsize\color{{gray}}{label}:{self.line}}}"
        if attributes:
            metadata += r"\qquad\text{\ttfamily\scriptsize " + textext(attributes) + "}"
        lines += [r"\\[2pt]" + metadata, r"\end{gathered}\]}"]
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
            try:
                latex = rule.latex()
            except ValueError as error:
                raise ValueError(f"{rule.source}:{rule.line}: {error}") from error
            pieces.append("```{=latex}\n" + latex + "\n```")
        flush()
        return "\n\n".join(pieces)

    rendered = BLOCKS.sub(block, text)
    return rendered, rules
