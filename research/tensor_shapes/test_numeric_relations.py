import unittest
import json
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import z3

from access_relations import Normal, Polynomial
from constraint_state import ConstraintState
from infer_types import Graph
from numeric_relations import NumericRelations
from sass.semantics.fp import softfloat
from word_ranges import Ranges
from test_infer_types import CORPUS


def reciprocal_graph():
    g = Graph()
    divisor = g.input('divisor', 32)
    converted = g.node('conversion', 32, divisor)
    g.numeric_operations[converted] = 'cvt.rp.f32.u32', (divisor,)
    reciprocal = g.node('primitive', 32, 'mufu_rcp', converted)
    scaled = g.node('add', 32, reciprocal, g.node('const', 32, 0x0ffffffe))
    integer = g.node('conversion', 32, scaled)
    g.numeric_operations[integer] = 'cvt.rzi.ntz.u32.f32', (scaled,)
    return g, divisor, converted, reciprocal, scaled, integer


def model(graph, context):
    analysis = SimpleNamespace(graph=graph, normal=Normal(graph), range_context=Ranges(context))
    return NumericRelations(analysis, ConstraintState())


class NumericRelationTests(unittest.TestCase):
    def test_normalized_arithmetic_keeps_small_values_after_intermediate_wrap(self):
        g = Graph()
        n, d = g.input('n', 32), g.input('d', 32)
        complement = g.node('not', 32, d)
        intermediate = g.node('add', 32, complement, n)
        result = g.node('add', 32, intermediate, d)
        for size in (1, 2, 127, 128, 129, 512):
            m = model(g, {'n:32': [size, size], 'd:32': [128, 128]})
            word = m.word(result)
            self.assertEqual(m.solver.check(), z3.sat)
            self.assertTrue(m.prove(word == size-1))

    def test_one_bit_constant_keeps_its_boolean_value(self):
        m = model(Graph(), {})
        self.assertTrue(m.prove(m.polynomial(Polynomial.constant(1, 1)) == 1))
        self.assertEqual(m.solver.check(), z3.sat)

    def test_two_integer_corrections_suffice_for_bounded_reciprocal_error(self):
        modulus = 1 << 32
        divisor, numerator, estimate, refined, quotient = z3.Reals('d n t b q')
        error = modulus-divisor*estimate
        remainder = numerator-divisor*quotient
        first = z3.If(remainder >= divisor, remainder-divisor, remainder)
        second = z3.If(first >= divisor, first-divisor, first)
        solver = z3.SolverFor('QF_NRA')
        solver.set(timeout=2000)
        solver.add(divisor >= 1, divisor <= modulus/2, numerator >= 0, numerator < modulus/2,
                   estimate >= 0, error >= 0, error <= divisor+8192, refined >= 0)
        solver.add(estimate*error-modulus*(refined-estimate) >= 0,
                   estimate*error-modulus*(refined-estimate) < modulus)
        solver.add(refined*numerator-modulus*quotient >= 0,
                   refined*numerator-modulus*quotient < modulus)
        solver.add(z3.Or(second < 0, second >= divisor))
        self.assertEqual(solver.check(), z3.unsat)

    def test_rounding_and_reciprocal_error_give_integer_multiplier_bounds(self):
        g, divisor, _, _, _, integer = reciprocal_graph()
        m = model(g, {'divisor:32': [1, (1 << 31)-1]})
        d, t = m.word(divisor), m.word(integer)
        self.assertEqual(m.solver.check(), z3.sat)
        self.assertTrue(m.prove(d*t <= 1 << 32))
        self.assertTrue(m.prove((1 << 32)-d*t <= d+8192))

    def test_zero_divisor_does_not_acquire_a_positive_input_assumption(self):
        g, _, _, _, _, integer = reciprocal_graph()
        m = model(g, {'divisor:32': [0, 0]})
        with self.assertRaises(NotImplementedError): m.word(integer)
        self.assertEqual(m.solver.check(), z3.sat)

    def test_contract_includes_representable_reciprocal_neighbors(self):
        g, divisor, converted, reciprocal, scaled, integer = reciprocal_graph()
        m = model(g, {'divisor:32': [1, (1 << 31)-1]})
        result = m.word(integer)
        values = sorted({x for p in range(31) for x in ((1 << p)-1, 1 << p, (1 << p)+1)
                         if 1 <= x < 1 << 31})
        for d in values:
            f = softfloat.pack(Fraction(d), 32, 'RP')
            fv = softfloat.unpack(f, 32)[2]
            r = softfloat.pack(1/fv, 32, 'RN')
            for rb in (r-1, r, r+1):
                bits = rb+0x0ffffffe
                value = int(softfloat.unpack(bits, 32)[2])
                m.solver.push()
                m.solver.add(m.word(divisor) == d, m.word(converted) == f,
                             z3.Real(f'f{converted.index}') == z3.RealVal(str(fv)),
                             m.word(reciprocal) == rb, m.word(scaled) == bits, result == value)
                self.assertEqual(m.solver.check(), z3.sat, (d, hex(rb)))
                m.solver.pop()

    def test_conversion_metadata_retains_exact_rounding_semantics(self):
        from infer_types import build
        from sass.semantics import bitvec, lifter
        from sass.semantics.families import convert
        from sass.semantics.fp import fpformat
        x = bitvec.reg('R', 0, 1)
        expression = convert.int_to_float('U32', fpformat.dest('F32'), fpformat.rounding('RP'),
                                          bitvec.const(31, 64), 32, x)
        sem = lifter.Semantics('SM90', 'test', 'conversion', 0, 'native',
                               writes=[(bitvec.reg('R', 1, 1), expression)])
        graph = build([sem])
        operations = list(graph.numeric_operations.values())
        self.assertEqual([op for op, _ in operations], ['cvt.rp.f32.u32'])
        self.assertTrue(all(value.op != 'cvt.rp.f32.u32' for value in graph.values))


@unittest.skipUnless(CORPUS.exists(), 'set SHAPE_CORPUS to the handwritten cubin corpus')
class CorpusNumericRelationTests(unittest.TestCase):
    def test_dot_quotient_error_is_derived_from_instruction_constraints(self):
        from common_constraints import CommonConstraints
        from constraint_domain import add, multiply
        from infer_types import kernel
        import memory_types
        context = json.loads(Path(__file__).with_name('experiment_contexts.json').read_text())['kc_dot_partial']
        inference, _ = memory_types.infer(kernel(CORPUS/'KC_dot_2pass/KC_dot_2pass_sm90.elf', 'kc_dot_partial'), context)
        a = CommonConstraints(inference, context)
        state = a.transfer(0, 0xd0, a.initial_state())
        after = a.refine_numeric_relations(state, 0xd0)
        graph = inference.graph
        divisor = a.value(graph.values[18])
        numerator = a.value(graph.values[71])
        quotient = a.value(graph.values[168])
        residual = add(add(numerator, multiply(divisor, quotient), -1), divisor, -1)
        self.assertLessEqual(after.query(residual), -1)
        self.assertTrue(state.contains(after))
        numerical = NumericRelations(a, a.numeric_context(state))
        result = numerical.word(graph.values[168])
        for size in (1, 2, 3, 4, 127, 128, 129, 512, 513):
            numerical.solver.push()
            for name, value in {'SR33': 0, 'SR37': 0, 'cbc': 1, 'cb0': 128, 'cb228': size}.items():
                numerical.solver.add(numerical.word(graph.inputs[name, 32]) == value)
            numerical.solver.add(result == (size-1)//128)
            self.assertEqual(numerical.solver.check(), z3.sat, size)
            numerical.solver.pop()
        without = NumericRelations(a, ConstraintState())
        with self.assertRaises(NotImplementedError): without.word(graph.values[134])


if __name__ == '__main__': unittest.main()
