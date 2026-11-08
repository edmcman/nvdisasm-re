import pytest

from sass.sleigh.build import ARCHES
from tests.harness import autocases, lifted


@pytest.mark.parametrize("arch", ARCHES)
def test_async_operand_forms(arch):
    from sass.decode import ir
    for k in ir.load(arch).classes:
        if k.alternate or k.mnemonic != "LDGSTS": continue
        fields = dict(sz="128", Ra=4, Rb=8)
        fields.update({n: r for n, r in (("Ra_URc", 6), ("Ra_URd", 13), ("Rb_URc", 12)) if n in k.operand_types})
        form = autocases.encoded_form(arch, k, fields)
        for case in autocases.generate(arch, k.name, form, sample=100):
            status, problems = lifted.run(arch, "exact", case)
            assert status == "native" and not problems, (case.id, problems)


@pytest.mark.parametrize("arch", ("SM89", "SM90"))
def test_sc_fence_events_have_reference_cases(arch):
    for sem in ("SC", "ALL", "nomembar_sem"):
        for scope in ("CTA", "SM", "GPU", "SYS"):
            cases = autocases.generate(arch, "membar_", dict(sem=sem, sco=scope), sample=100)
            assert cases
            for case in cases:
                assert case.hardware["skip"]
                assert not lifted.run(arch, "exact", case)[1]
