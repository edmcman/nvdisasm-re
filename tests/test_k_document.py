"""Check that publication preserves executable rules, including conditions."""
import re

import pytest

from sass.formal import k
from sass.formal.k import render


@pytest.mark.parametrize("source", k.SOURCES, ids=lambda source: source.name)
def test_rule_source_correspondence(source):
    original = source.read_text()
    published, rules = render.publication(source)
    lines = original.splitlines()
    # Independently count source rules and conditions, including continuation lines.
    assert len(rules) == len(re.findall(r"^\s*rule ", original, re.MULTILINE))
    expected_conditions = sum(
        len(re.findall(r"\brequires\b", render.outside_strings(block)))
        for block in re.findall(r"^```k\n(.*?)^```", original, re.MULTILINE | re.DOTALL)
    ) - len(re.findall(r'^requires "', original, re.MULTILINE))
    assert sum(bool(rule.parts()[1]) for rule in rules) == expected_conditions
    for rule in rules:
        span = lines[rule.line - 1:rule.line - 1 + len(rule.text.splitlines())]
        assert rule.text == "\n".join(span).rstrip()
        conclusion, condition, attributes = rule.parts()
        reconstructed = conclusion
        if condition:
            reconstructed += " requires " + condition
        if attributes:
            reconstructed += " " + attributes
        assert reconstructed.split() == rule.text.split()[1:]
        assert rule.latex() in published
        assert render.textext(conclusion).count(r"\Longrightarrow") == len(
            re.findall("=>", render.outside_strings(conclusion)))


def test_literal_tokens_and_attributes():
    rule = render.Rule("example.md", 1,
                       'rule <k> "requires =>" => "done" ... </k>\n'
                       '     requires V =/=Int 0 [priority(40)]')
    conclusion, condition, attributes = rule.parts()
    assert condition == "V =/=Int 0"
    assert attributes == "[priority(40)]"
    assert render.textext(conclusion).count(r"\Longrightarrow") == 1
    assert '"requires =\\textgreater{}"' in render.textext(conclusion)
    assert render.Rule("example.md", 2, "rule getInt(M, K) => M[K]").parts()[2] == ""


def test_natural_width_and_literal_tex_characters():
    latex = render.Rule("example.md", 1, "rule X => Y requires X >Int 0").latex()
    assert r"\frac{" in latex
    assert r"\linewidth" not in latex and "tcolorbox" not in latex
    assert render.textext(r"{$_%&#~^\}") == (
        r"\{\$\_\%\&\#\textasciitilde{}\textasciicircum{}\textbackslash{}\}")
    # A long literal must not be split across formula rows.
    literal = '"' + "a " * 70 + '"'
    assert literal in render.formula(literal)


def test_wrapped_rhs_stays_in_replacement_column():
    _, rules = render.publication(k.DEFINITION.with_name("float.md"))
    rule = next(rule for rule in rules
                if rule.text.startswith('  rule roundBump(Q, REM, D, "RN", _)'))
    latex = rule.latex()
    # All replacement rows belong to the left-aligned third column after the arrow.
    replacement = latex.split(r"&\Longrightarrow&", 1)[1]
    assert replacement.startswith(r"\begin{array}[t]{@{}l@{}}")
    assert "orBool" in replacement
    assert r"\\\text{\ttfamily\footnotesize ((2 *Int REM" in replacement
    assert "roundBump(Q, REM, D" not in replacement
