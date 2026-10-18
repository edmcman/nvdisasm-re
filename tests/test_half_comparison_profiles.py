"""The smaller input table must retain audited distinctions, aliases, and FTZ witnesses."""
import json
from pathlib import Path
from research import audit_shared_cases
from tests.harness import autocase_half_profiles


def decode_key(key):
    op,fmt,ftz,cmp,bop,pnot,sa,sb,output=key.split('/')
    return op,fmt,bool(int(ftz)),cmp,bop,bool(int(pnot)),sa,sb,output


def test_reduced_inputs_preserve_fault_distinctions():
    original=tuple((j,j&1) for j in range(8))
    for key,samples in autocase_half_profiles.table().items():
        sig=decode_key(key)
        assert len(samples)==len(set(map(tuple,samples)))
        assert {p for _,p in samples}=={0,1}
        assert {j&1 for j,_ in samples}=={0,1}, 'alias and separate placements must remain'
        assert all(0<=j<8 and p in (0,1) for j,p in samples)
        baseline=tuple(audit_shared_cases.result(sig,j) for j in range(8))
        selected=tuple(audit_shared_cases.result(sig,j,p_override=p) for j,p in samples)
        before=audit_shared_cases.fault_outputs(sig,original)
        after=audit_shared_cases.fault_outputs(sig,samples)
        for name,outputs in before.items():
            if outputs!=baseline:
                assert after[name]!=selected,(key,name)
        # In exact FTZ forms, require a witness whenever the same numeric inputs
        # can distinguish flushing with either predicate value.
        if sig[1]=='F16' and sig[2]:
            other=audit_shared_cases.replace(sig,2,False)
            available=any(audit_shared_cases.result(sig,j,p_override=p)!=
                          audit_shared_cases.result(other,j,p_override=p)
                          for j in range(8) for p in (0,1))
            if available:
                assert after['FTZ:toggle']!=selected,key


def test_input_table_regenerates_from_audit(tmp_path):
    path=Path(autocase_half_profiles.__file__).with_name('data')/'half_comparison_inputs.json'
    expected=json.loads(json.dumps(audit_shared_cases.input_table()))
    assert json.loads(path.read_text())==expected
    generated=tmp_path/"inputs.json"
    audit_shared_cases.write_inputs(generated)
    assert json.loads(generated.read_text())==expected


def test_unmasked_subnormal_seed_is_kept():
    form={'cmp':'LT','bop':'AND','bval':'BM','Pp@not':'set','ftz':'FTZ',
          'iswzA':'H1_H0','iswzB_as_C':'H1_H0'}
    assert [1,0] in autocase_half_profiles.select('HSET2',form,'exact')
