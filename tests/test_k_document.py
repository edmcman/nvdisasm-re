"""Check publication leaves executable source and rule provenance intact."""
import re

import pytest

from sass.formal import k
from sass.formal.k import render


@pytest.mark.parametrize("source", k.SOURCES, ids=lambda source: source.name)
def test_rule_source_correspondence(source):
    original = source.read_text()
    published, rules = render.publication(source)
    assert published == original
    lines = original.splitlines()
    assert len(rules) == len(re.findall(r"^\s*rule ", original, re.MULTILINE))
    for rule in rules:
        span = lines[rule.line - 1:rule.line - 1 + len(rule.text.splitlines())]
        assert rule.text == "\n".join(span).rstrip()
        assert rule.text in published


def test_literal_tokens_conditions_and_attributes(tmp_path):
    source = tmp_path / "example.md"
    text = ('# Example\n\n```k\n'
            '  rule <k> "requires =>" => "done" ... </k>\n'
            '       requires V =/=Int 0 [priority(40)]\n'
            '  rule getInt(M, K) => M[K] [owise]\n'
            '```\n')
    source.write_text(text)
    published, rules = render.publication(source)
    assert published == text
    assert rules[0].line == 4 and rules[1].line == 6
    assert 'requires V =/=Int 0 [priority(40)]' in rules[0].text
    assert '"requires =>"' in rules[0].text
    assert '[owise]' in rules[1].text
