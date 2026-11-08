import argparse
import ctypes
import gzip
import hashlib
import json
from pathlib import Path

import numpy

from sass.emulate import build
from research.mufu import reciprocal_fit

SOURCE = """
#include "sass/mufu.hpp"
extern "C" uint64_t verify_reciprocal(const uint32_t *hardware, uint32_t count,
                                    uint32_t base, bool high, bool rsq) {
  uint64_t failures = 0;
  for (uint32_t i = 0; i < count; ++i) {
    uint32_t source = base + i;
    uint32_t result = high ? sass::mufu::reciprocal<64>(uint64_t(source) << 32, rsq) >> 32
                           : sass::mufu::reciprocal<32>(source, rsq);
    failures += result != hardware[i];
  }
  return failures;
}
"""


def verify(path, report, native):
    capture = json.loads(path.read_text())
    payload = (path.parent / capture['deltas']).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == capture['sha256']
    raw = gzip.decompress(payload)
    assert len(raw) == capture['count'] * 4
    hardware = numpy.cumsum(numpy.frombuffer(raw, dtype='<i4'), dtype=numpy.int64)
    count = 1 << capture['mantissa_bits']
    offsets = numpy.arange(count, dtype=numpy.int64)
    width = capture['mantissa_bits'] - (7 if capture['op'] == 'RCP' else 6)
    findings = []
    for i, parity in enumerate(capture['parities']):
        finding = next(f for f in report['findings'] if
                       (f['op'], f['precision'], f['exponent_parity']) == (capture['op'], capture['precision'], parity))
        rounding = 'nearest_even' if capture['precision'] == 'F32' else 'toward_zero'
        model = next(m for m in finding['integer_models'] if
                     m['coefficient_grid'] == [57, 42, 40] and m['final_rounding'] == rounding)
        coefficients = numpy.array(model['coefficients'], dtype=numpy.int64)[offsets >> width]
        accumulator = reciprocal_fit.fixed_accumulator(coefficients, offsets & ((1 << width) - 1),
                                                       width, model['coefficient_grid'])
        got = reciprocal_fit.words(accumulator, capture['precision'], rounding, 57)
        if capture['op'] == 'RSQ' and parity == 0:
            got[0] = 0x3f800000 if capture['precision'] == 'F32' else 0x3ff00000
        observed = hardware[i * count:(i + 1) * count].astype(numpy.uint32)
        failures = numpy.flatnonzero(got != observed)
        native_failures = native(observed.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)), count,
                                 (capture['exponent_bias'] + parity) << capture['mantissa_bits'],
                                 capture['precision'] == '64H', capture['op'] == 'RSQ')
        result = dict(parity=parity, n=count, model_mismatches=len(failures), runtime_mismatches=native_failures,
                      examples=[dict(offset=int(offsets[x]), got=int(got[x]), hardware=int(observed[x]))
                                for x in failures[:5]])
        findings.append(result)
        print(path.name, result, flush=True)
    return dict(capture=path.name, sha256=capture['sha256'], arch=capture['arch'], precision=capture['precision'],
                op=capture['op'], findings=findings)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('captures', type=Path, nargs='+')
    parser.add_argument('--models', type=Path, default=Path('research/mufu/reciprocal/models.json'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.models.read_text())
    library = build.library(SOURCE, 'reciprocal_verify')
    native = library.verify_reciprocal
    native.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_bool, ctypes.c_bool]
    native.restype = ctypes.c_uint64
    findings = [verify(path, report, native) for path in args.captures]
    args.output.write_text(json.dumps(dict(models_sha256=hashlib.sha256(args.models.read_bytes()).hexdigest(),
                                         findings=findings), indent=1) + '\n')
    if any(f['model_mismatches'] or f['runtime_mismatches'] for r in findings for f in r['findings']):
        raise SystemExit(1)


if __name__ == '__main__': main()
