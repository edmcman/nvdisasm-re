import os
import unittest
from pathlib import Path

from sass.semantics import bitvec, lifter
from sass.semantics.family import Flow, float_neg
from infer_types import Graph, Inference, build, kernel
from type_system import ADDRESS, NUMERIC, InferenceError, Scheme, Solver, arrow, dimension, floating, integer, pointer, shape


class UnificationTests(unittest.TestCase):
    def test_alias_and_nested_shape(self):
        s = Solver()
        a, b, extent = s.fresh(width=64), s.fresh(width=64), s.fresh("dimension")
        p = pointer("global", 64, floating(32), shape(extent, dimension(8)))
        s.unify(a, b, "copy")
        s.unify(b, p, "load")
        s.unify(extent, dimension("M"), "signature")
        self.assertEqual(s.resolve(a), pointer("global", 64, floating(32), shape(dimension("M"), dimension(8))))

    def test_occurs_check(self):
        s = Solver()
        a = s.fresh()
        with self.assertRaisesRegex(InferenceError, "occurs check.*recursive"):
            s.unify(a, arrow(a, floating(32)), "recursive")

    def test_incompatible_types_dimensions_and_widths(self):
        for a, b in [(floating(32), integer(32)), (shape(dimension(8)), shape(dimension(9))),
                     (shape(dimension(8)), shape(dimension(8), dimension(9))), (floating(32), floating(64))]:
            with self.subTest(a=a, b=b), self.assertRaises(InferenceError): Solver().unify(a, b, "test")
        s = Solver()
        with self.assertRaises(InferenceError): s.unify(s.fresh("shape"), floating(32), "sort")

    def test_polymorphic_identity_instantiates_independently(self):
        s = Solver()
        a = s.fresh()
        identity = Scheme(frozenset([a]), arrow(a, a))
        one, two = identity.instantiate(s), identity.instantiate(s)
        p = pointer("global", 64, floating(32), s.fresh("shape"))
        s.unify(one.args[0], floating(32), "float use")
        s.unify(two.args[0], p, "pointer use")
        self.assertEqual(s.resolve(one.args[1]), floating(32))
        self.assertEqual(s.resolve(two.args[1]), p)


class InferenceTests(unittest.TestCase):
    def test_projected_join_keeps_fields_independent(self):
        g = Graph()
        tid, x = g.input("SR33", 32), g.input("x", 32)
        g.node("fadd", 32, x, g.node("const", 32, 0), "RN")
        a = g.node("concat", 64, x, tid)
        b = g.node("concat", 64, x, g.node("const", 32, 32))
        merged = g.node("phi", 64, a, b)
        high, low = g.extract(merged, 63, 32), g.extract(merged, 31, 0)
        p = g.input("p", 64)
        g.accesses.append(("write", "ram", p, high, None, "stored high field"))
        g.node("shl", 32, low, g.node("const", 32, 1))
        i = Inference(g).generate().solve()
        self.assertEqual(i.solver.resolve(i.types[p]).args[1], floating(32))
        self.assertEqual(i.solver.resolve(i.types[low]), integer(32))

    def test_common_index_does_not_equate_buffer_shapes(self):
        g = Graph()
        x, y, index = g.input("x", 64), g.input("y", 64), g.input("index", 32)
        offset = g.node("zext", 64, index)
        addresses = [g.node("add", 64, p, offset) for p in (x, y)]
        loads = [g.node("load", 32, "ram", a) for a in addresses]
        g.node("fadd", 32, *loads, "RN")
        i = Inference(g).generate().solve()
        px, py = (i.solver.resolve(i.types[v]) for v in (x, y))
        self.assertEqual(px.args[1], floating(32))
        self.assertNotEqual(px.args[2], py.args[2])
        self.assertEqual(i.solver.resolve(i.types[index]), integer(32))

    def test_ambiguous_add_is_residual_not_a_choice(self):
        g = Graph()
        a, b = g.input("a", 64), g.input("b", 64)
        address = g.node("add", 64, a, b)
        g.node("load", 32, "ram", address)
        i = Inference(g).generate().solve()
        self.assertNotEqual(i.solver.root(i.roles[a]), ADDRESS)
        self.assertNotEqual(i.solver.root(i.roles[b]), ADDRESS)
        self.assertTrue(any(r["kind"] == "arithmetic_overload" for r in i.residuals))

    def test_pointer_addition_is_order_independent(self):
        for reverse in (False, True):
            g = Graph()
            p, index = g.input("p", 64), g.input("i", 32)
            offset = g.node("zext", 64, index)
            args = (offset, p) if reverse else (p, offset)
            address = g.node("add", 64, *args)
            loaded = g.node("load", 32, "ram", address)
            g.node("fmul", 32, loaded, g.node("const", 32, 0), "RN", 0)
            i = Inference(g).generate().solve()
            self.assertEqual(i.solver.resolve(i.types[p]).args[1], floating(32))
            self.assertEqual(i.solver.resolve(i.types[p]), i.solver.resolve(i.types[address]))

    def test_annotation_is_checked(self):
        g = Graph()
        p = g.input("p", 64)
        loaded = g.node("load", 32, "ram", p)
        g.node("fadd", 32, loaded, g.node("const", 32, 0), "RN")
        i = Inference(g).generate()
        i.annotate("p", 64, pointer("global", 64, integer(32), shape(dimension("N"))))
        with self.assertRaisesRegex(InferenceError, "annotation p:64"): i.solve()

    def test_float_bits_have_an_explicit_integer_view(self):
        g = Graph()
        x = g.input("x", 32)
        g.node("fadd", 32, x, g.node("const", 32, 0), "RN")
        g.node("shl", 32, x, g.node("const", 32, 1))
        i = Inference(g).generate().solve()
        self.assertEqual(i.solver.resolve(i.types[x]), floating(32))
        self.assertTrue(any(v["value"] == x.index and v["storage"] == "f32" and v["as"] == "i32" for v in i.report()["integer_views"]))

    def test_shared_memory_linkage_remains_explicit(self):
        g = Graph()
        a = g.input("offset", 32)
        g.node("load", 32, "shared", a)
        i = Inference(g).generate().solve()
        self.assertEqual(i.accesses, [])
        self.assertEqual(i.residuals[0]["kind"], "memory_cell")

    def test_supplied_shape_propagates_to_access(self):
        g = Graph()
        p = g.input("p", 64)
        a = g.node("add", 64, p, g.node("const", 64, 4))
        g.node("load", 32, "ram", a)
        i = Inference(g).generate()
        layout = shape(dimension("M"), dimension("N"))
        i.annotate("p", 64, pointer("global", 64, floating(32), layout)).solve()
        self.assertEqual(i.solver.resolve(i.accesses[0]["shape"]), layout)


class RepresentationTests(unittest.TestCase):
    def test_exact_slices_reassemble_only_same_source(self):
        g = Graph()
        a, b = g.input("a", 64), g.input("b", 64)
        hi, lo = g.extract(a, 63, 32), g.extract(a, 31, 0)
        self.assertIs(g.concat((hi, lo)), a)
        self.assertIsNot(g.concat((g.extract(b, 63, 32), lo)), a)

    def test_carry_fold_requires_unsigned_word_offset(self):
        for offset, folds in [(4, True), ((1 << 32) + 4, False)]:
            g = Graph()
            p = g.input("p", 64)
            lo, hi = g.extract(p, 31, 0), g.extract(p, 63, 32)
            summed = g.node("add", 64, g.node("zext", 64, lo), g.node("const", 64, offset))
            carry = g.node("zext", 32, g.node("ule", 1, g.node("const", 64, 1 << 32), summed))
            combined = g.concat((g.node("add", 32, hi, carry), g.extract(summed, 31, 0)))
            self.assertEqual(combined.op, "add" if folds else "concat")
            if folds: self.assertIs(combined.args[0], p)

    def test_register_reuse_does_not_merge_pointer_with_float(self):
        r = lambda n, count=1: bitvec.reg("R", n, count)
        cb = bitvec.load("cbank", bitvec.const(0x210, 64), 64)
        sems = [
            lifter.Semantics("SM90", "test", "pointer", 0, "native", writes=[(r(2, 2), cb)]),
            lifter.Semantics("SM90", "test", "load and reuse low word", 16, "native", writes=[(r(2), bitvec.load("ram", r(2, 2), 32))]),
            lifter.Semantics("SM90", "test", "float", 32, "native", writes=[(r(2), bitvec.op("fadd", r(2), bitvec.const(0, 32), "RN", w=32))]),
        ]
        g = build(sems)
        i = Inference(g).generate().solve()
        self.assertEqual(i.solver.resolve(i.types[g.inputs["cb210", 64]]).args[1], floating(32))

    def test_typed_float_negation_survives_lowering(self):
        r = bitvec.reg("R", 0, 1)
        x = bitvec.load("cbank", bitvec.const(0x210, 64), 32)
        sems = [lifter.Semantics("SM90", "test", "negate float", 0, "native", writes=[(r, float_neg(x))])]
        g = build(sems)
        i = Inference(g).generate().solve()
        self.assertEqual(i.solver.resolve(i.types[g.inputs["cb210", 32]]), floating(32))

    def test_unreachable_register_reuse_is_ignored(self):
        r = bitvec.reg("R", 0, 1)
        sems = [
            lifter.Semantics("SM90", "test", "return", 0, "native", flow=Flow("return", bitvec.const(0, 64))),
            lifter.Semantics("SM90", "test", "unreachable call", 16, "native", writes=[(r, bitvec.const(0, 32))], flow=Flow("call", bitvec.const(16, 64))),
        ]
        self.assertEqual(len(build(sems).control), 1)

    def test_predicated_use_selects_reaching_definition(self):
        r = lambda n: bitvec.reg("R", n, 1)
        guard = bitvec.reg("P", 0, 1, element=1)
        for guarded_use in (True, False):
            sems = [
                lifter.Semantics("SM90", "test", "integer definition", 0, "native", writes=[(r(0), bitvec.reg("SR", 33, 1))]),
                lifter.Semantics("SM90", "test", "predicated float definition", 16, "native", guard=guard, writes=[(r(0), bitvec.load("cbank", bitvec.const(0x210, 64), 32))]),
                lifter.Semantics("SM90", "test", "float use", 32, "native", guard=guard if guarded_use else None,
                                writes=[(r(1), bitvec.op("fadd", r(0), bitvec.const(0, 32), "RN", w=32))]),
            ]
            g = build(sems)
            i = Inference(g).generate()
            if guarded_use:
                i.solve()
                self.assertEqual(i.solver.resolve(i.types[g.inputs["cb210", 32]]), floating(32))
                self.assertEqual(i.solver.resolve(i.types[g.inputs["SR33", 32]]), integer(32))
            else:
                with self.assertRaises(InferenceError): i.solve()

    def test_dead_predicated_merge_does_not_unify_definitions(self):
        r = bitvec.reg("R", 0, 1)
        x = bitvec.load("cbank", bitvec.const(0x210, 64), 32)
        sems = [
            lifter.Semantics("SM90", "test", "float definition", 0, "native", writes=[(r, float_neg(x))]),
            lifter.Semantics("SM90", "test", "unused predicated reuse", 16, "native", guard=bitvec.reg("P", 0, 1, element=1), writes=[(r, bitvec.reg("SR", 33, 1))]),
        ]
        g = build(sems)
        i = Inference(g).generate().solve()
        self.assertEqual(i.solver.resolve(i.types[g.inputs["cb210", 32]]), floating(32))

    def test_word_carry_identity_on_boundaries(self):
        mask = (1 << 32) - 1
        for hi in (0, 1, mask):
            for lo in (0, 1, mask - 1, mask):
                for offset in (0, 1, 4, mask):
                    summed = lo + offset
                    split = (((hi + int(summed >= 1 << 32)) & mask) << 32) | (summed & mask)
                    whole = (((hi << 32) | lo) + offset) & ((1 << 64) - 1)
                    self.assertEqual(split, whole)

    def test_loop_phi_carries_pointer_type(self):
        r = lambda n, count=1: bitvec.reg("R", n, count)
        cb = bitvec.load("cbank", bitvec.const(0x210, 64), 64)
        sems = [
            lifter.Semantics("SM90", "test", "pointer", 0, "native", writes=[(r(2, 2), cb)], flow=Flow("goto", bitvec.const(16, 64))),
            lifter.Semantics("SM90", "test", "float load", 16, "native", writes=[(r(0), bitvec.op("fadd", bitvec.load("ram", r(2, 2), 32), bitvec.const(0, 32), "RN", w=32))]),
            lifter.Semantics("SM90", "test", "advance", 32, "native", writes=[(r(2, 2), r(2, 2) + bitvec.const(4, 64))], flow=Flow("goto", bitvec.const(16, 64))),
        ]
        g = build(sems)
        i = Inference(g).generate().solve()
        self.assertEqual(i.solver.resolve(i.types[g.inputs["cb210", 64]]).args[1], floating(32))


CORPUS = Path(os.environ.get("SHAPE_CORPUS", Path(__file__).resolve().parents[2] / "third_party/culifter/test/cuda_handwritten"))


@unittest.skipUnless(CORPUS.exists(), "set SHAPE_CORPUS to the handwritten cubin corpus")
class CorpusTests(unittest.TestCase):
    def infer(self, name):
        g = kernel(CORPUS / name / f"{name}_sm90.elf")
        i = Inference(g).generate().solve()
        return g, i

    def test_axpy_signature(self):
        g, i = self.infer("KC_axpy")
        pointers = [i.solver.resolve(i.types[g.inputs[f"cb{offset:x}", 64]]) for offset in (0x210, 0x218)]
        self.assertEqual([p.args[1] for p in pointers], [floating(32)] * 2)
        self.assertNotEqual(pointers[0].args[2], pointers[1].args[2])
        self.assertEqual(i.solver.resolve(i.types[g.inputs["cb220", 32]]), floating(32))
        self.assertEqual(i.solver.resolve(i.types[g.inputs["cb224", 32]]), integer(32))
        self.assertEqual(len(i.accesses), 3)

    def test_matrix_inputs_without_assumed_rank(self):
        for name in ("KC_sgemm_naive", "KC_gemv_n_simple"):
            with self.subTest(name=name):
                g, i = self.infer(name)
                shapes = []
                for offset in (0x210, 0x218, 0x220):
                    p = i.solver.resolve(i.types[g.inputs[f"cb{offset:x}", 64]])
                    self.assertEqual(p.args[1], floating(32))
                    shapes.append(p.args[2])
                self.assertEqual(len(set(shapes)), 3)

    def test_reused_registers_in_reduction_kernels(self):
        for name in ("KC_asum_simple", "KC_nrm2_unrolled"):
            with self.subTest(name=name):
                g, i = self.infer(name)
                p = i.solver.resolve(i.types[g.inputs["cb210", 64]])
                self.assertEqual(p.args[1], floating(32))


if __name__ == "__main__": unittest.main()
