import argparse
import hashlib
import itertools
import json
import subprocess
from pathlib import Path

from sass import paths
from tests.harness import autocases, device_cases
from tests.harness.case import Cases


def forms(request):
    match request:
        case 'R1':
            return [('hadd2_F32__RR', dict(Ra='RZ', **{'Ra@negate': 1},
                                         iswzB_as_C=sw, ftz=ftz))
                    for sw, ftz in itertools.product(('H0_H0', 'H1_H1'), ('noftz', 'FTZ'))]
        case 'R2':
            ordinary = [('hadd2__RR', dict(ftz='FTZ', sat=sat)) for sat in ('nosat', 'SAT')]
            ordinary += [('hfma2__RRR', dict(fmz='FTZ', satrelu=sat)) for sat in ('nosat', 'SAT')]
            return ordinary + [('hadd2__RR', dict(ftz='FTZ', iswzA=sw, iswzB_as_C=sw,
                                                 **{'Ra@absolute': absolute, 'Ra@negate': negate,
                                                    'Rc@negate': 1}))
                               for sw in ('H1_H0', 'H0_H0', 'H1_H1')
                               for absolute, negate in ((1, 0), (0, 1), (1, 1))]
        case 'R3':
            return [('hfma2_mma__RRR_relu' if sat == 'RELU' else 'hfma2_mma__RRR_norelu', dict(fmz='FTZ', satrelu=sat))
                    for sat in ('nosat', 'SAT', 'RELU')] + [
                        ('hmul2__RR', dict(fmz=mode)) for mode in ('FTZ', 'FMZ')]
        case 'R4' | 'R8':
            classes = (('hmnmx2__RRR', 'Rb'), ('hmnmx2__RCR', 'Sb'),
                       ('hmnmx2__RUR', 'URb'), ('hmnmx2__RCxR', 'Sb'),
                       ('hset2__RC', 'Sc'), ('hsetp2__RC', 'Sc'))
            return [(cls, dict(**{n+'@absolute': absolute, n+'@negate': negate,
                                 'iswzB': sw, 'Pp@not': inverted}))
                    for cls, n in classes if request == 'R4' or n in ('Sb', 'Sc')
                    for sw, inverted in itertools.product(('H0_H0', 'H1_H1'), (0, 1))
                    for absolute, negate in ((0, 0), (1, 0), (0, 1), (1, 1))]
        case 'R6':
            return [(cls, {}) for cls in ('hadd2__RR', 'hfma2__RRR', 'hfma2_mma__RRR_norelu')]
        case 'R7':
            return [('hfma2__RRI', {'Ra@negate': 1, 'Sc': 0, 'Sc1': 0})]
        case 'R9':
            return forms('R6') + [('hadd2_F32__RR', dict(Ra='RZ', **{'Ra@negate': 1},
                                                       iswzB_as_C='H1_H1')),
                                 ('hsetp2__RR', dict(cmp='GT', bop='AND'))]
        case 'R10':
            return [('f2i__Rb_16b', dict(srcfmt='F16', ftz='FTZ', rnd='TRUNC', ntz='NTZ', hsel='H0'))]
        case 'R12':
            return [('hfma2__RRR', dict(ofmt='BF16_V2')),
                    ('hfma2_relu__RRR', dict(ofmt='BF16_V2', satrelu='RELU'))]
    raise ValueError(request)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('requests', nargs='+')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    tools = {name: dict(path=str(path), sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest())
             for name, path in (('nvcc', paths.NVCC), ('ptxas', paths.PTXAS), ('nvdisasm', paths.NVDISASM))}
    results = dict(origin='synthetic autocase encodings', tools=tools,
                   revision=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                   requests={})
    for request in args.requests:
        cases = Cases('SM89')
        for cls, fields in forms(request):
            k = cases.classes[cls]
            if 'iswzB' in fields and 'iswzB' not in k.operand_types:
                fields['iswzB_as_C'] = fields.pop('iswzB')
            form = autocases.encoded_form('SM89', k, fields)
            cases.extend(autocases.generate('SM89', cls, form, discovery=True))
        cases[:] = {c.id: c for c in cases}.values()
        print(request, len(cases), flush=True)
        result = device_cases.observe(cases)
        result['inputs'] = {c.id: c.request for c in cases}
        results['requests'][request] = result
        args.out.write_text(json.dumps(results))
        print(request, 'captured', flush=True)


if __name__ == '__main__':
    main()
