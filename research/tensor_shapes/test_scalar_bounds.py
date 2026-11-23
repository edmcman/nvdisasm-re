import unittest
from dataclasses import replace

from scalar_bounds import WordRange
from word_ranges import Interval
from test_infer_types import CORPUS
from test_common_shapes import corpus
from sass.decode import cubin
from sass.semantics import evaluate, lifter


def members(word):
    return [v for v in range(word.interval.lower, word.interval.upper + 1)
            if (v - word.residue) % word.modulus == 0] if word.modulus else [word.residue]


def covers(word, value):
    return word.interval.lower <= value <= word.interval.upper and (value == word.residue if not word.modulus else (value - word.residue) % word.modulus == 0)


class WordRangeTests(unittest.TestCase):
    def test_small_width_transfers_overapproximate_every_value(self):
        domains = [WordRange(Interval(lo, hi), stride, residue)
                   for lo, hi in ((0, 15), (1, 7), (8, 15), (0, 4)) for stride in (1, 2, 4) for residue in range(stride)]
        domains += [WordRange.constant(v, 4) for v in range(16)]
        for a in domains:
            for coefficient in (-16, -4, -1, 0, 1, 3, 8):
                scaled = a.scale(coefficient, 4)
                for value in members(a): self.assertTrue(covers(scaled, value * coefficient % 16))
            for b in domains:
                joined, added, multiplied = a.join(b), a.add(b, 4), a.multiply(b, 4)
                for x in members(a):
                    self.assertTrue(covers(joined, x))
                    for y in members(b):
                        self.assertTrue(covers(added, (x + y) % 16))
                        self.assertTrue(covers(multiplied, x * y % 16))

    def test_restriction_respects_congruence(self):
        domain = WordRange(Interval(0, 128), 4, 0)
        self.assertEqual(domain.restrict(Interval(13, 127)).interval, Interval(16, 124))
        self.assertIsNone(domain.restrict(Interval(1, 3)))


@unittest.skipUnless(CORPUS.exists(), 'set SHAPE_CORPUS to the handwritten cubin corpus')
class ScalarBoundsTests(unittest.TestCase):
    def test_common_scalar_fixed_point_covers_every_successor(self):
        analysis = corpus('KC_sgemm_naive', 'kc_sgemm_naive').analysis
        self.assertIsNotNone(analysis.numeric)
        self.assertTrue(analysis.numeric.verify())

    def test_scalar_domains_cover_concrete_iterations(self):
        analysis = corpus('KC_sgemm_naive', 'kc_sgemm_naive').analysis
        bounds = analysis.numeric
        self.assertIsNotNone(bounds)
        data = (CORPUS/'KC_sgemm_naive/KC_sgemm_naive_sm90.elf').read_bytes()
        image = cubin.kernel_image(data, 'kc_sgemm_naive')
        sems = [lifter.lift(image['arch'], word, k*16, fp='readable') for k, word in enumerate(cubin.words(image['code']))]
        checked = 0
        for reduction in range(1, 129):
            state = evaluate.State()
            for register, value in ((33, 2), (34, 1), (37, 0), (38, 0)):
                state.write('SR', register, 1, value)
            for offset, width, value in ((0, 32, 8), (4, 32, 8), (0x210, 64, 0x100000),
                                         (0x218, 64, 0x200000), (0x220, 64, 0x300000),
                                         (0x230, 32, 17), (0x234, 32, 4), (0x238, 32, reduction)):
                state.store('cbank', offset, width, value)
            pc = 0
            for _ in range(4000):
                for atom, phi, hi, lo in bounds.projections.get(pc, []):
                    actual = analysis.graph.read_phi(phi, state.read) >> lo & ((1 << (hi-lo+1))-1)
                    domain = bounds.word(atom, bounds.states[pc])
                    self.assertTrue(covers(domain, actual), (reduction, phi.index, actual, domain))
                    checked += 1
                sem = sems[pc//16]
                if sem.flow and sem.flow.kind == 'return' and (sem.guard is None or evaluate.evaluate(sem.guard, state)): break
                evaluate.apply(replace(sem, effects=[]), state)
                pc = state.pc
            else: self.fail('concrete trace did not terminate')
        self.assertGreater(checked, 1000)


if __name__ == '__main__': unittest.main()
