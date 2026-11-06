import random
import unittest
from infer_types import Graph, build
from access_relations import Atom, Normal, Polynomial, invariant
from sass.semantics import bitvec, lifter

def interpret(term, inputs):
    mask = (1 << term.width) - 1
    match term:
        case Polynomial(_, terms):
            total = 0
            for (factors, coefficient) in terms:
                product = coefficient
                for a in factors:
                    product *= interpret(a, inputs)
                total += product
            return total & mask
        case Atom('input', _, [name]):
            return inputs[name] & mask
        case Atom('coordinate', _, [name]):
            return inputs[name] & mask
        case Atom('phi', _, [index]):
            return inputs[f'%{index}'] & mask
        case Atom('and', _, [a, b]):
            return interpret(a, inputs) & interpret(b, inputs)
        case Atom('word', _, [source]):
            value = interpret(source, inputs)
            return (value - (1 << source.width) if value >= 1 << source.width - 1 else value) & mask
        case Atom('extract', _, [source, _, lo]):
            return interpret(source, inputs) >> lo & mask
        case Atom('zext', _, [source]):
            return interpret(source, inputs)
        case Atom('sext', _, [source]):
            value = interpret(source, inputs)
            return (value - (1 << source.width) if value >= 1 << source.width - 1 else value) & mask
        case _:
            raise TypeError(term)

class AlgebraTests(unittest.TestCase):

    def test_modular_normal_form_matches_machine_arithmetic(self):
        rng = random.Random(14)
        for width in (8, 16, 32, 64):
            g = Graph()
            (x, y) = (g.input('x', width), g.input('y', width))
            expression = g.node('sub', width, g.node('mul', width, g.node('add', width, x, y), x), y)
            poly = Normal(g)(expression)
            for _ in range(30):
                (a, b) = (rng.getrandbits(width), rng.getrandbits(width))
                self.assertEqual(interpret(poly, {'x': a, 'y': b}), ((a + b) * a - b) % (1 << width))

    def test_signed_extension_is_not_distributed_over_wrapping_add(self):
        g = Graph()
        (a, b) = (g.input('a', 32), g.input('b', 32))
        narrow_sum = g.node('add', 32, a, b)
        extend_after = g.node('sext', 64, narrow_sum)
        sum_after = g.node('add', 64, g.node('sext', 64, a), g.node('sext', 64, b))
        n = Normal(g)
        self.assertNotEqual(n(extend_after), n(sum_after))
        values = {'a': 2147483647, 'b': 1}
        self.assertEqual(interpret(n(extend_after), values), (1 << 64) - (1 << 31))
        self.assertEqual(interpret(n(sum_after), values), 1 << 31)

    def test_low_word_projection_through_phi_and_register_reuse(self):
        g = Graph()
        (m, other) = (g.input('M', 32), g.input('other', 32))
        phi = g.node('phi', 64)
        first = g.node('concat', 64, other, m)
        next_value = g.node('concat', 64, g.node('opaque_high', 32), g.extract(phi, 31, 0))
        phi.args = (first, next_value)
        n = Normal(g)
        self.assertEqual(n(g.extract(phi, 31, 0)), n(m))
        self.assertTrue(invariant(n(g.extract(phi, 31, 0))))
        self.assertFalse(invariant(n(g.extract(phi, 63, 32))))

    def test_changing_projected_word_is_not_invariant(self):
        g = Graph()
        m = g.input('M', 32)
        phi = g.node('phi', 32)
        phi.args = (m, g.node('add', 32, phi, g.node('const', 32, 1)))
        n = Normal(g)
        self.assertFalse(invariant(n(phi)))
        self.assertNotEqual(n(phi), n(m))

    def test_recursive_expression_is_not_an_alias(self):
        g = Graph()
        phi = g.node('phi', 32)
        phi.args = (g.node('add', 32, phi, g.node('const', 32, 1)),)
        n = Normal(g)
        self.assertFalse(invariant(n(phi)))

class FlowTests(unittest.TestCase):

    def test_unknown_special_register_is_not_a_loop_invariant_input(self):
        sems = [lifter.Semantics('SM90', 'test', 'special', address, 'native', writes=[(bitvec.reg('R', 0, 1), bitvec.reg('SR', 80, 1))]) for address in (0, 16)]
        g = build(sems)
        reads = [v for v in g.values if v.op == 'special']
        self.assertEqual(len(reads), 2)
        self.assertFalse(invariant(Normal(g)(reads[0])))
if __name__ == '__main__':
    unittest.main()
