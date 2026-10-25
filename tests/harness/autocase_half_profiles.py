"""Audited declarative input selections for packed comparisons.

The table stores seed indices and raw predicate values, never expected results.
Regenerate with research.audit_shared_cases; the adapter still uses independent
references for every selected case.
"""
import json
from functools import cache
from pathlib import Path
from tests.harness import autocase_profiles


@cache
def table():
    data = json.loads((Path(__file__).with_name('data') / 'half_comparison_inputs.json').read_text())
    inputs = {fmt: list(autocase_profiles.PROFILES['half_forms'][fmt]) for fmt in ('F16', 'BF16')}
    if data['version'] != 1 or data['inputs'] != inputs:
        raise ValueError('half-comparison inputs changed; regenerate the audited selections')
    return data['forms']


def select(op, form, fp):
    from tests.harness import autocases
    signature = (op, form.get('ofmt', 'F16_V2').removesuffix('_V2'),
                 int(fp == 'exact' and form.get('ftz') == 'FTZ'), form['cmp'], form['bop'],
                 int(form.get('Pp@not') == 'set'), form.get('iswzA', 'H1_H0'),
                 form.get('iswzB', form.get('iswzB_as_C', 'H1_H0')),
                 form.get('bval', form.get('h_and')))
    key = '/'.join(map(str, signature))
    try:
        return table()[key]
    except KeyError as error:
        raise autocases.Unsupported('missing audited half-comparison input selection: ' + key) from error
