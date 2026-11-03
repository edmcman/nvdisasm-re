import argparse
import gzip
import hashlib
import json
import random
import struct
import time
from functools import cache
from pathlib import Path
from tests.harness import autocases, device_cases
from tests.harness.autocase_half_requests import Inputs
from sass.decode import ir


@cache
def configuration(arch, op, precision, extract):
    cls = 'mufu_fp16__RR' if precision == 'F16' else 'mufu__RRR_RR'
    k = next(k for k in ir.load(arch).classes if k.name == cls)
    field = 'mufuop' if 'mufuop' in k.operand_types else 'op'
    fields = {field: op + ('64H' if precision == '64H' else '')}
    if precision == 'F16':
        fields.update({'width' if 'width' in k.operand_types else 'fmt': 'F16', 'extract': extract})
    form = autocases.encoded_form(arch, k, fields)
    return cls, form


def observe_values(arch, op, precision, extract, values):
    cls, form = configuration(arch, op, precision, extract)
    cases = autocases.generate(arch, cls, form, discovery=True,
                              input_profile=Inputs(values=tuple(values), destination=8))
    result = device_cases.observe(cases)
    words = []
    for item in result['observations']:
        if 'error' in item: raise RuntimeError(item)
        words.append(item['registers']['R8'])
    return words, result['device']


def capture(arch, op, precision, extract, values, output):
    cls, form = configuration(arch, op, precision, extract)
    observations, device = [], None
    start = time.monotonic()
    for first in range(0, len(values), 65536):
        words, device = observe_values(arch, op, precision, extract, values[first:first + 65536])
        observations.extend(words)
        label = extract if precision == 'F16' else precision
        print(f'{op}.{label}: {len(observations)}/{len(values)} ({time.monotonic() - start:.1f}s)', flush=True)
    data = dict(arch=arch, device=device, op=op, precision=precision, extract=extract, form=form, cls=cls,
                inputs=values, outputs=observations)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(gzip.compress(json.dumps(data, separators=(',', ':')).encode(), mtime=0))


def capture_normalized(arch, op, precision, output):
    mantissa, bias = (23, 127) if precision == 'F32' else (20, 1023)
    parities = (0,) if op == 'RCP' else (0, 1)
    cls, form = configuration(arch, op, precision, 'H0')
    output.parent.mkdir(parents=True, exist_ok=True)
    delta_path = output.with_suffix('.deltas.i32.gz')
    start, previous, count, device = time.monotonic(), 0, 0, None
    with delta_path.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as compressed:
        for parity in parities:
            for first in range(0, 1 << mantissa, 65536):
                values = range(((bias + parity) << mantissa) + first,
                               ((bias + parity) << mantissa) + min(first + 65536, 1 << mantissa))
                words, device = observe_values(arch, op, precision, 'H0', values)
                differences = [word - last for last, word in zip([previous] + words[:-1], words, strict=True)]
                compressed.write(struct.pack(f'<{len(differences)}i', *differences))
                previous, count = words[-1], count + len(words)
                if count % (1 << 20) == 0:
                    print(f'{op}.{precision}: {count}/{len(parities) << mantissa} ({time.monotonic() - start:.1f}s)', flush=True)
    output.write_text(json.dumps(dict(arch=arch, device=device, op=op, precision=precision, cls=cls, form=form,
        mantissa_bits=mantissa, exponent_bias=bias, parities=parities, count=count,
        deltas=delta_path.name, sha256=hashlib.sha256(delta_path.read_bytes()).hexdigest()), indent=1) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--arch', default='SM89')
    parser.add_argument('--op', choices=('RCP', 'RSQ'), required=True)
    parser.add_argument('--extract', choices=('H0', 'H1'), default='H0')
    parser.add_argument('--precision', choices=('F16', 'F32', '64H'), default='F16')
    parser.add_argument('--count', type=int, default=65536)
    parser.add_argument('--offsets', type=int, nargs='+', default=[0])
    parser.add_argument('--random', type=int, metavar='SEED')
    parser.add_argument('--full-normalized', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.full_normalized:
        if args.precision == 'F16': parser.error('--full-normalized requires F32 or 64H')
        capture_normalized(args.arch, args.op, args.precision, args.output)
        return
    match args.precision:
        case 'F16': values = list(range(args.count))
        case fmt:
            from tests.harness.autocase_profiles import PROFILES
            mantissa, bias, maximum = (23, 127, 255) if fmt == 'F32' else (20, 1023, 2047)
            stride = max(1, (1 << mantissa) // args.count)
            fractions = (random.Random(args.random).sample(range(1 << mantissa), args.count)
                         if args.random is not None else range(0, 1 << mantissa, stride))
            normalized = [exp << mantissa | fraction + offset for exp in (bias, bias + 1)
                          for fraction in fractions for offset in args.offsets if 0 <= fraction + offset < 1 << mantissa]
            transitions = [word + offset for exp in range(1, maximum) for word in (exp << mantissa,)
                           for offset in (-1, 0, 1)]
            values = list(dict.fromkeys(normalized + transitions
                          + list(PROFILES['mufu_reciprocal']['words' if fmt == 'F32' else 'high_words'])))
    capture(args.arch, args.op, args.precision, args.extract, values, args.output)


if __name__ == '__main__': main()
