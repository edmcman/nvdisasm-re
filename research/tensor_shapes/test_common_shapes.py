import json
import unittest
from functools import lru_cache
from pathlib import Path

from access_relations import Atom
from common_shapes import Access, CommonShapes, decompose
from constraint_domain import multiply
from constraint_state import ConstraintState
from infer_types import Graph, Inference, kernel
import memory_types
from test_infer_types import CORPUS


def guarded_vector(negative=False, names=('data', 'index', 'length')):
    g = Graph()
    base, index, extent = (g.input(name, width) for name, width in zip(names, (64, 32, 32)))
    zero = g.node('const', 32, 0)
    bound = g.node('slt', 1, index, extent)
    sign = g.node('slt', 1, index, zero) if negative else g.node('sle', 1, zero, index)
    guard = g.node('or' if negative else 'and', 1, bound, sign)
    offset = g.node('mul', 64, g.node('sext', 64, index), g.node('const', 64, 4))
    value = g.node('fadd', 32, zero, zero, 'RN')
    g.accesses.append(('write', 'ram', g.node('add', 64, base, offset), value, guard, 'store'))
    g.blocks = {0: {'successors': [], 'predecessors': []}}
    g.source_blocks['store'] = 0
    return Inference(g).generate().solve()


class DimensionTests(unittest.TestCase):
    def test_symbolic_euclidean_remainder_borrows_from_quotient(self):
        n, remaining, column = ((Atom('input', 64, (name,)),) for name in ('N', 'remaining', 'column'))
        state = ConstraintState().assume(inequalities=[({remaining: -1}, -1), ({remaining: 1, n: -1}, 0)])
        access = Access({}, state, {}, [], {})
        index = {**multiply({column: 1}, {n: 1}), n: 1, remaining: -1}
        quotient, remainder = decompose(access, index, {n: 1})
        self.assertEqual(quotient, {column: 1})
        self.assertEqual(remainder, {n: 1, remaining: -1})

    def test_bounds_give_extent_independent_of_names(self):
        for names in (('data', 'index', 'length'), ('x', 'j', 'Q')):
            report = CommonShapes(guarded_vector(names=names)).report()
            tensor, = report['tensors']
            self.assertEqual(tensor['dimensions'], [f'{names[2]}:32'])
            self.assertTrue(tensor['dimensions_recovered'])
            self.assertFalse(tensor['integer_interpretation_proven'])

    def test_negative_disjunct_is_not_silently_certified(self):
        report = CommonShapes(guarded_vector(negative=True)).report()
        self.assertFalse(any(t['dimensions_recovered'] for t in report['tensors']))

    def test_absent_bounds_remain_unresolved(self):
        i = guarded_vector()
        for access in i.accesses: access['guard'] = None
        report = CommonShapes(i).report()
        self.assertFalse(any(t['dimensions_recovered'] for t in report['tensors']))


@lru_cache(maxsize=None)
def corpus(folder, entry):
    context = json.loads(Path(__file__).with_name('experiment_contexts.json').read_text())[entry]
    path = CORPUS / folder / f'{folder}_sm90.elf'
    inference, _ = memory_types.infer(kernel(path, entry), context)
    return CommonShapes(inference, context)


@unittest.skipUnless(CORPUS.exists(), 'set SHAPE_CORPUS to the handwritten cubin corpus')
class CorpusDimensionTests(unittest.TestCase):
    def test_dot_both_vectors_and_grid_output(self):
        report = corpus('KC_dot_2pass', 'kc_dot_partial').report()
        self.assertEqual({t['buffer']: t['dimensions'] for t in report['tensors']},
                         {'cb210': ['cb228:32'], 'cb218': ['cb228:32'], 'cb220': ['cbc:32']})
        self.assertTrue(all(t['dimensions_recovered'] for t in report['tensors']))
        self.assertTrue(all(c['upper_bound_proven'] and c['nonnegative_proven']
                            for t in report['tensors'] for c in t['coordinate_checks']))
        self.assertTrue(report['fixed_point_verified'])
        self.assertFalse(report['integer_interpretation_proven'])

    def test_triangular_solve_both_matrix_arguments(self):
        report = corpus('KC_strsm_lower_8x8', 'kc_strsm_lower_8x8').report()
        self.assertEqual({t['buffer']: t['dimensions'] for t in report['tensors']},
                         {'cb210': ['8', '8'], 'cb218': ['8', '8']})
        self.assertTrue(all(t['dimensions_recovered'] for t in report['tensors']))
        self.assertTrue(all(c['upper_bound_proven'] and c['nonnegative_proven']
                            for t in report['tensors'] for c in t['coordinate_checks']))
        self.assertEqual(report['residuals'], [])
        self.assertTrue(report['fixed_point_verified'])
        self.assertFalse(report['integer_interpretation_proven'])

    def test_reduction_and_grid_output_use_same_domain(self):
        report = corpus('KC_nrm2_simple', 'kc_nrm2_partial').report()
        self.assertEqual({t['buffer']: t['dimensions'] for t in report['tensors']},
                         {'cb210': ['cb220:32'], 'cb218': ['cbc:32']})
        self.assertTrue(all(t['dimensions_recovered'] for t in report['tensors']))

    def test_naive_gemm_all_three_argument_dimensions(self):
        report = corpus('KC_sgemm_naive', 'kc_sgemm_naive').report()
        self.assertEqual({t['buffer']: t['dimensions'] for t in report['tensors']},
                         {'cb210': ['cb230:32', 'cb234:32'],
                          'cb218': ['cb230:32', 'cb238:32'],
                          'cb220': ['cb238:32', 'cb234:32']})
        self.assertTrue(all(t['dimensions_recovered'] for t in report['tensors']))

    def test_tiled_gemm_all_three_arguments_without_tile_handlers(self):
        report = corpus('KC_sgemm_tiled_smem', 'kc_sgemm_tiled').report()
        self.assertEqual({t['buffer']: t['dimensions'] for t in report['tensors']},
                         {'cb210': ['cb230:32', 'cb234:32'],
                          'cb218': ['cb230:32', 'cb238:32'],
                          'cb220': ['cb238:32', 'cb234:32']})
        self.assertEqual(report['residuals'], [])
        self.assertTrue(all(t['dimensions_recovered'] for t in report['tensors']))
        self.assertTrue(report['fixed_point_verified'])
        self.assertFalse(report['integer_interpretation_proven'])


if __name__ == '__main__': unittest.main()
