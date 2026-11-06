import unittest

from access_relations import Atom
from constraint_domain import Equalities, add, multiply


def variable(name): return (Atom("variable", 64, (name,)),)


class EqualityDomainTests(unittest.TestCase):
    def test_join_retains_relationship_and_forgets_individual_values(self):
        x, y = variable("x"), variable("y")
        left = Equalities.make([{x: 1, (): -2}, {y: 1, (): -8}])
        right = Equalities.make([{x: 1, (): -7}, {y: 1, (): -3}])
        joined = left.join(right)
        self.assertFalse(joined.reduce({x: 1, y: 1, (): -10}))
        self.assertTrue(joined.reduce({x: 1, (): -2}))
        self.assertEqual(joined, right.join(left))

    def test_parallel_transfer_derives_conservation_without_loop_patterns(self):
        x, y = variable("progress"), variable("remaining")
        state = Equalities.make([{x: 1, y: 1, (): -100}])
        incoming = state.image({x: {x: 1, (): 4}, y: {y: 1, (): -4}, (): {(): 1}})
        self.assertFalse(incoming.reduce({x: 1, y: 1, (): -100}))

    def test_different_transfer_does_not_preserve_false_conservation(self):
        x, y = variable("x"), variable("y")
        state = Equalities.make([{x: 1, y: 1, (): -100}])
        incoming = state.image({x: {x: 1, (): 5}, y: {y: 1, (): -4}, (): {(): 1}})
        self.assertTrue(incoming.reduce({x: 1, y: 1, (): -100}))

    def test_conditional_integer_domain_does_not_claim_modular_soundness(self):
        x = variable("x")
        state = Equalities.make([{x: 2}])
        self.assertFalse(state.reduce({x: 1}))
        # This conclusion is valid over rationals/integers. A caller must separately
        # establish the integer interpretation of bitvector operations.
        self.assertFalse(state.inconsistent)

    def test_polynomial_features_are_linear_columns_not_candidate_invariants(self):
        x, m = variable("x"), (Atom("input", 32, ("M",)),)
        product = multiply({x: 1}, {m: 1})
        self.assertEqual(len(next(iter(product))), 2)
        self.assertEqual(add(product, product, -1), {})


if __name__ == "__main__": unittest.main()
