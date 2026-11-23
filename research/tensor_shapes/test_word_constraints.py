import unittest

from access_relations import Atom, Normal
from common_constraints import CommonConstraints
from common_shapes import CommonShapes
from constraint_domain import add
from constraint_state import ConstraintState
from infer_types import Graph, Inference
from test_access_relations import interpret
from test_common_constraints import loop


def store_graph(index, guard=None):
    g = index[0]
    base = g.input('data', 64)
    address = g.node('add', 64, base, g.node('mul', 64, g.node('zext', 64, index[1]), g.node('const', 64, 4)))
    zero = g.node('const', 32, 0)
    value = g.node('fadd', 32, zero, zero, 'RN')
    g.accesses.append(('write', 'ram', address, value, guard, 'store'))
    g.blocks = {0: {'successors': [], 'predecessors': []}}
    g.source_blocks['store'] = 0
    return Inference(g).generate().solve()


class WordConstraintTests(unittest.TestCase):
    def test_polynomial_subsumption_uses_the_meaning_of_products(self):
        atom = Atom('input', 32, ('x',))
        x, square = (atom,), (atom, atom)
        incoming = ConstraintState().assume(inequalities=[({x: -1}, 0), ({x: 1}, 3)])
        target = ConstraintState().assume(inequalities=[({square: 1, x: -3}, 0)])
        self.assertTrue(target.contains(incoming))
        wider = ConstraintState().assume(inequalities=[({x: -1}, 0), ({x: 1}, 4)])
        self.assertFalse(target.contains(wider))

    def test_bitwise_complement_is_exact_modulo_word_width(self):
        for width in (2, 4, 8):
            g = Graph()
            x = g.input('x', width)
            source = g.node('add', width, g.node('mul', width, x, g.node('const', width, 3)), g.node('const', width, 5))
            complement = Normal(g)(g.node('not', width, source))
            for value in range(1 << width):
                self.assertEqual(interpret(complement, {'x': value}), ~(3 * value + 5) & ((1 << width) - 1))

    def test_predicates_zero_extension_and_sign_extension_are_distinct(self):
        g = Graph()
        flag = g.input('flag', 1)
        yes = g.node('const', 1, 1)
        zero = g.node('zext', 32, flag)
        signed = g.node('sext', 32, flag)
        inverse = g.node('not', 1, flag)
        g.blocks = {0: {'successors': [], 'predecessors': []}}
        a = CommonConstraints(Inference(g).generate().solve())
        self.assertEqual(a.value(yes), {(): 1})
        self.assertEqual(a.value(zero), a.value(flag))
        self.assertEqual(a.value(signed), {m: -c for m, c in a.value(flag).items()})
        self.assertEqual(a.value(inverse), add({(): 1}, a.value(flag), -1))

    def test_low_bits_reduce_to_affine_expression_inside_one_period(self):
        g = Graph()
        x = g.input('SR33', 32)
        masked = g.node('and', 32, x, g.node('const', 32, 3))
        a = CommonConstraints(store_graph((g, masked)))
        source, remainder = a.value(x), a.value(masked)
        before = ConstraintState().assume(inequalities=[(source, 7), ({m: -c for m, c in source.items()}, -4)])
        after = a.numeric_reduce(before, 0)
        self.assertFalse(after.reduce(add(add(remainder, source, -1), {(): 4})))
        self.assertEqual(after, a.numeric_reduce(after, 0))

    def test_integer_quotient_rounding_preserves_low_bit_relationship(self):
        g = Graph()
        x = g.input('SR33', 32)
        masked = g.node('and', 32, x, g.node('const', 32, 3))
        a = CommonConstraints(store_graph((g, masked)))
        source, remainder = a.value(x), a.value(masked)
        before = ConstraintState().assume(inequalities=[(source, 100), ({m: -c for m, c in source.items()}, -4)])
        after = a.numeric_reduce(before, 0)
        self.assertLessEqual(after.query(add(remainder, source, -1)), -4)

    def test_bitwise_expression_survives_only_while_its_ssa_inputs_survive(self):
        g, (_, _, _, progress, _, _) = loop()
        g.blocks[1]['successors'] = [1, 3]
        g.blocks[2]['predecessors'] = [0, 3]
        g.blocks[3] = {'successors': [2], 'predecessors': [1]}
        g.control[1]['fallthrough'] = 3
        masked = g.node('and', 32, progress, g.node('const', 32, 3))
        a = CommonConstraints(Inference(g).generate().solve())
        expression = a.value(masked)
        feature, = expression
        self.assertEqual(a.substitute_edge(feature, 1, 3), expression)
        self.assertEqual(next(iter(a.substitute_edge(feature, 1, 1)))[0].operation, 'havoc')
        self.assertEqual(next(iter(a.substitute_edge(next(iter(a.value(progress))), 0, 2)))[0].operation, 'havoc')

    def test_inclusive_program_bound_is_preferred_to_launch_extent(self):
        g = Graph()
        g.arch = 'SM90'
        index = g.input('SR33', 32)
        guard = g.node('ule', 1, index, g.node('const', 32, 7))
        report = CommonShapes(store_graph((g, index), guard)).report()
        tensor, = report['tensors']
        self.assertEqual(tensor['dimensions'], ['8'])
        self.assertTrue(tensor['dimensions_recovered'])
        self.assertFalse(tensor['integer_interpretation_proven'])

    def test_shared_condition_preserves_relationship_between_two_assignments(self):
        g = Graph()
        x = g.input('SR33', 32)
        condition = g.node('slt', 1, x, g.node('const', 32, 8))
        y = g.node('ite', 32, condition, g.node('add', 32, x, g.node('const', 32, 1)), x, origin='select_y')
        z = g.node('ite', 32, condition, g.node('sub', 32, g.node('const', 32, 11), x), g.node('sub', 32, g.node('const', 32, 12), x), origin='select_z')
        inference = store_graph((g, g.node('add', 32, y, z)))
        g.blocks = {0: {'successors': [1], 'predecessors': []}, 1: {'successors': [], 'predecessors': [0]}}
        g.source_blocks.update(select_y=0, select_z=0, store=1)
        a = CommonConstraints(inference).solve()
        self.assertFalse(a.states[1].reduce(add(add(a.value(y), a.value(z)), {(): -12})))
        self.assertTrue(a.verify())


if __name__ == '__main__': unittest.main()
