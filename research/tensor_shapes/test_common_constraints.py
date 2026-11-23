import unittest
from fractions import Fraction

from access_relations import Atom, Polynomial
from common_constraints import CommonConstraints
from constraint_domain import Equalities, add, multiply
from constraint_state import ConstraintState
from infer_types import Graph, Inference
from linear_bounds import LinearBounds


def feature(name): return (Atom('input', 64, (name,)),)


def loop(step=1, names=('base', 'M', 'N')):
    g = Graph()
    base, stride, size = (g.input(name, width) for name, width in zip(names, (64, 32, 32)))
    zero, amount = g.node('const', 32, 0), g.node('const', 32, step)
    progress, remaining, address = (g.node('phi', width) for width in (32, 32, 64))
    progress.args = zero, g.node('add', 32, progress, amount)
    remaining.args = size, g.node('sub', 32, remaining, amount)
    delta = g.node('mul', 64, g.node('sext', 64, stride), g.node('const', 64, 4 * step))
    address.args = base, g.node('add', 64, address, delta)
    g.blocks = {0: {'successors': [1, 2], 'predecessors': []},
                1: {'successors': [1, 2], 'predecessors': [0, 1]},
                2: {'successors': [], 'predecessors': [0, 1]}}
    for value in (progress, remaining, address):
        g.phi_blocks[value], g.phi_predecessors[value] = 1, [0, 1]
    for block, condition in ((0, g.node('slt', 1, zero, size)),
                             (1, g.node('slt', 1, amount, remaining))):
        g.control.append({'block': block, 'kind': 'goto', 'target': 1, 'fallthrough': 2,
                          'guard': None, 'condition': condition.index, 'origin': 'branch'})
    # Observing both counters makes them part of the finite feature vocabulary.
    g.control[1]['condition'] = g.node('and', 1, g.values[g.control[1]['condition']],
                                      g.node('slt', 1, progress, size)).index
    value = g.node('fadd', 32, g.node('const', 32, 0), g.node('const', 32, 0), 'RN')
    g.accesses.append(('write', 'ram', address, value, None, 'access'))
    g.source_blocks['access'] = 1
    return g, (base, stride, size, progress, remaining, address)


class LinearBoundTests(unittest.TestCase):
    def test_long_chain_is_solved_without_search_depth_cutoff(self):
        xs = [feature(str(i)) for i in range(8)]
        bounds = [(tuple({a: 1, b: -1}.items()), Fraction(-1)) for a, b in zip(xs, xs[1:])]
        bounds.append((((xs[-1], 1),), Fraction(20)))
        self.assertEqual(LinearBounds(bounds).upper({xs[0]: 1}), 13)

    def test_unbounded_is_not_a_dimension(self):
        x = feature('x')
        self.assertIsNone(LinearBounds([]).upper({x: 1}))

    def test_contradictory_paths_are_bottom(self):
        x, n = feature('x'), feature('n')
        state = ConstraintState().assume(inequalities=[({x: 1, n: -1}, -1)])
        self.assertIsNone(state.assume(inequalities=[({n: 1, x: -1}, 0)]))

    def test_projection_uses_equalities_and_all_bounds(self):
        p, r, n = map(feature, ('progress', 'remaining', 'N'))
        state = ConstraintState(Equalities.make([{p: 1, r: 1, n: -1}]))
        state = state.assume(inequalities=[({r: -1}, -4)])
        self.assertEqual(state.query({p: 1, n: -1}), -4)

    def test_join_covers_both_predecessors(self):
        x = feature('x'); row = ((x, 1),)
        left = ConstraintState().assume(inequalities=[({x: 1}, 3)])
        right = ConstraintState().assume(inequalities=[({x: 1}, 7)])
        joined = left.join(right, [row])
        self.assertEqual(joined.query({x: 1}), 7)
        self.assertTrue(joined.contains(left) and joined.contains(right))


class CommonConstraintTests(unittest.TestCase):
    def test_positive_symbolic_factor_cancels_before_integer_rounding(self):
        g, (_, stride, size, _, _, _) = loop(4)
        a = CommonConstraints(Inference(g).generate().solve())
        quotient = Atom('word', 64, (Polynomial.atom(Atom('shr', 32, (a.normal(size), Polynomial.constant(32, 2)))),))
        a.quotients['test'] = quotient
        difference = {(a.loop_counts[1],): 1, (quotient,): -1}
        a.templates[1].append(tuple(difference.items()))
        factor = a.value(stride)
        state = ConstraintState().assume(inequalities=[(multiply(difference, factor), -1),
                                                       ({m: -c for m, c in factor.items()}, -1)])
        after = a.refine_iterations(state, 1)
        self.assertLessEqual(after.query(difference), -1)
        negative = ConstraintState().assume(inequalities=[(multiply(difference, factor), -1), (factor, -1)])
        self.assertIsNone(a.refine_iterations(negative, 1).query(difference))

    def test_iteration_count_retains_symbolic_address_progression(self):
        for step in (1, 3, 8):
            g, (base, stride, _, progress, _, address) = loop(step)
            a = CommonConstraints(Inference(g).generate().solve()).solve()
            count = {(a.loop_counts[1],): 1}
            self.assertFalse(a.states[1].reduce(add(a.value(progress), count, -step)))
            expected = multiply(count, a.value(stride))
            self.assertFalse(a.states[1].reduce(add(add(a.value(address), a.value(base), -1), expected, -4 * step)))
            self.assertTrue(a.verify())

    def test_interval_product_distinguishes_before_and_after_assignment(self):
        g, (_, _, size, progress, remaining, _) = loop(3)
        a = CommonConstraints(Inference(g).generate().solve(), {'N:32': [30, 30], 'M:32': [2, 2]}).solve()
        before = ConstraintState().assume([add(a.value(remaining), {(): -30}), a.value(progress)])
        after = a.transfer(1, 1, before)
        self.assertIsNotNone(after)
        self.assertEqual(after.query(a.value(remaining)), 27)
        self.assertTrue(a.verify())

    def test_symbolic_stride_and_countdown_use_one_transfer_and_join(self):
        for step in (1, 3, 8):
            for names in (('base', 'M', 'N'), ('data', 'pitch', 'length')):
                with self.subTest(step=step, names=names):
                    g, (base, stride, size, progress, remaining, address) = loop(step, names)
                    a = CommonConstraints(Inference(g).generate().solve()).solve()
                    self.assertTrue(a.verify())
                    state = a.states[1]
                    conservation = add(add(a.value(progress), a.value(remaining)), a.value(size), -1)
                    offset = multiply(a.value(progress), a.value(stride))
                    pointer = add(add(a.value(address), a.value(base), -1), offset, -4)
                    self.assertFalse(state.reduce(conservation))
                    self.assertFalse(state.reduce(pointer))
                    self.assertFalse(a.report()['integer_interpretation_proven'])

    def test_nonlinear_unknown_is_havoc_across_edges(self):
        g, (_, _, _, progress, _, _) = loop()
        a = CommonConstraints(Inference(g).generate().solve())
        unknown = next(iter(a.atom(Atom('unhandled', 32, (progress.index,)))))[0]
        image = a.substitute_edge((unknown,), 1, 1)
        self.assertEqual(next(iter(image))[0].operation, 'havoc')
        a.observe(1, image)
        self.assertFalse(any(x.operation == 'havoc' for m in a.features[1] for x in m))

    def test_guard_equality_is_closed_under_existing_polynomial_features(self):
        g, _ = loop()
        a = CommonConstraints(Inference(g).generate().solve())
        n, remainder, stride = (
            Atom('input', 32, (name,)) for name in ('N', 'tail', 'M'))
        # Use actual invariant word atoms to exercise the product-domain reduction.
        n, remainder, stride = (Atom('word', 64, (Polynomial.atom(x),)) for x in (n, remainder, stride))
        row = {(n,): 1, (remainder,): -1}
        features = {(), (n,), (remainder,), (stride,)} | set(multiply(row, {(stride,): 1}))
        state = Equalities.make(a.lift_equalities([row], features))
        self.assertFalse(state.reduce(multiply(row, {(stride,): 1})))


if __name__ == '__main__': unittest.main()
