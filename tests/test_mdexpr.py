"""md size-expression cases that control register dependencies."""
import unittest
from sass import ir, mdexpr


class MdExpressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.arch = ir.load('SM89')

    def test_nested_ternary_arms(self):
        expr = 'a ? b ? 3 : 2 : c ? 1 : 0'
        for values, expected in [({'a': 1, 'b': 1}, 3), ({'a': 1, 'b': 0}, 2),
                                 ({'a': 0, 'c': 1}, 1), ({'a': 0, 'c': 0}, 0)]:
            self.assertEqual(mdexpr.evaluate(expr, self.arch, values), expected)

    def test_mask_population_span(self):
        expr = 'ceil((((mask & 1) + ((mask >> 1) & 1) + ((mask >> 2) & 1)) * 32)/32)'
        for mask in range(8):
            self.assertEqual(mdexpr.evaluate(expr, self.arch, {'mask': mask}), mask.bit_count())

    def test_enum_and_boolean(self):
        self.assertEqual(mdexpr.evaluate('r == `Register@RZ && !flag ? 0 : 1', self.arch,
                                         {'r': 255, 'flag': 0}), 0)
        self.assertEqual(mdexpr.evaluate('r == `Register@RZ && !flag ? 0 : 1', self.arch,
                                         {'r': 255, 'flag': 1}), 1)

    def test_dotted_operand(self):
        self.assertEqual(mdexpr.free_vars(self.arch, 'size.ext ? 2 : 1'), {'size.ext'})
        self.assertEqual(mdexpr.evaluate('size.ext ? 2 : 1', self.arch, {'size.ext': 1}), 2)


if __name__ == '__main__':
    unittest.main()
