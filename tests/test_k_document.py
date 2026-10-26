"""Check that publication preserves executable rules, including conditions."""
import re

import pytest

from sass.formal import k
from sass.formal.k import notation, render


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
        # Independently check every expression's source tokens survive parsing,
        # including typed patterns, collections, map operations, and quoted text.
        cells = re.findall(r"<([A-Za-z]\w*)>(.*?)</\1>", conclusion, re.DOTALL)
        bodies = [body.strip().removeprefix("...").removesuffix("...").strip()
                  for _, body in cells] if cells else [conclusion]
        for body in [*bodies, condition] if condition else bodies:
            arrows = list(re.finditer("=>", render.outside_strings(body)))
            expressions = ([body[:arrows[0].start()], body[arrows[0].end():]]
                           if arrows else [body])
            for expression in expressions:
                parsed = notation.parse(expression)
                assert notation.tokens(parsed.k()) == notation.tokens(expression)


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
    assert r"\lor" in replacement and r"\land" in replacement
    assert r"\mathbf{1}\bigl[" in replacement
    assert "roundBump(Q, REM, D" not in replacement
    # Force a narrow layout: every continuation remains inside the RHS array.
    rhs = notation.parse(rule.parts()[0].split("=>", 1)[1])
    assert len(notation.rows(rhs, 20)) > 1
    assert r"\\" in render.formula(rhs, width=20, align="l")


def test_k_grouping_survives_mathematical_precedence():
    # K gives both operators one precedence, unlike conventional Boolean notation.
    term = notation.parse("A orBool B andBool C")
    assert term.value == "andBool" and term.children[0].value == "orBool"
    assert term.tex().startswith(r"\bigl(")
    assert r"\bigr) \land" in term.tex()
    power = notation.parse("A ^Int B ^Int C")
    assert power.value == "^Int" and power.children[0].value == "^Int"
    assert r"\bigl(" in power.tex() and r"\bigr)" in power.tex()
    # Exponentiation outranks multiplication; arithmetic outranks comparisons.
    term = notation.parse("2 *Int R >Int D")
    assert term.value == ">Int" and term.children[0].value == "*Int"
    term = notation.parse("notBool A ==Int 0")
    assert term.kind == "prefix" and term.children[0].value == "==Int"


def test_mathematical_notation_keeps_semantic_distinctions():
    assert r"\div_{\!t}" in notation.parse("A /Int D").tex()
    assert r"\mathrm{mod}_{\!E}" in notation.parse("A modInt D").tex()
    assert r"\mapsto" in notation.parse("(K |-> V) REST").tex()
    assert r"\leftarrow" in notation.parse("M[K <- V]").tex()
    assert r"\operatorname{dom}" in notation.parse("K in_keys(M)").tex()
    assert r"\mathbin{\triangleright}\mathrm{Int}" in notation.parse("{M[K]}:>Int").tex()
    assert r":\mathrm{Expr}" in notation.parse("A:Expr").tex()
    assert r'\text{\ttfamily "orBool +Int"}' in notation.parse('"orBool +Int"').tex()
    assert r"\right\rangle_{\mathrm{status}}" in render.Rule(
        "example.md", 1, 'rule <status> _ => "halted" </status>').latex()
    for invalid in ("A /Real B", "A ^%Int B C", "f(A", "A ; B", "A divInt B", "A impliesBool B"):
        with pytest.raises(ValueError):
            notation.parse(invalid)


def test_wrapping_respects_propositions_and_cell_scope():
    term = notation.parse('OP ==String "add" orBool OP ==String "sub" orBool OP ==String "mul"')
    rows = notation.rows(term, 15)
    assert len(rows) > 1
    assert all(not row.endswith(r"\mathit{OP}") for row in rows)
    rule = render.Rule("example.md", 1,
                       "rule <k> dispatch(instruction(A, G, E, W, S, F)) => next ... </k>"
                       " <pc> _ => PC +Int 16 </pc>")
    latex = rule.latex()
    assert latex.count(r"\left\langle") == 2
    assert latex.count(r"\right\rangle") == 2
    assert latex.count(r"\multicolumn{3}{c}") == 2
