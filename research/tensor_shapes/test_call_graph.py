import unittest

import call_graph
from infer_types import Graph, Inference, build, kernel
from type_system import InferenceError
from test_infer_types import CORPUS
from sass.semantics import bitvec, lifter
from sass.semantics.family import Flow


def sem(address, name, writes=(), flow=None):
    return lifter.Semantics("SM90", "test", name, address, "native", writes=list(writes), flow=flow)


def register(index, count=1): return bitvec.reg("R", index, count)


def expanded(sems):
    instructions, checks, contexts = call_graph.expand(sems)
    graph = build(instructions, checks)
    graph.call_contexts = contexts
    return graph


class CallGraphTests(unittest.TestCase):
    def test_local_call_reconnects_only_after_proving_return_address(self):
        sems = [sem(0, "return address", [(register(4), bitvec.const(32, 32))]),
                sem(16, "call", flow=Flow("call", bitvec.const(64, 64))),
                sem(32, "after call", [(register(0), bitvec.const(7, 32))]),
                sem(48, "exit", flow=Flow("return", bitvec.const(0, 64))),
                sem(64, "return", flow=Flow("return", bitvec.zext(register(4), 64)))]
        g = expanded(sems)
        self.assertEqual(len(g.verified_returns), 1)
        self.assertEqual(g.verified_returns[0]["return_target"], 32)
        self.assertTrue(any(c["original_address"] == 32 and not c["return_context"] for c in g.call_contexts.values()))

    def test_wrong_or_unknown_return_address_is_rejected(self):
        for value in (bitvec.const(99, 32), bitvec.reg("SR", 33, 1)):
            sems = [sem(0, "return address", [(register(4), value)]),
                    sem(16, "call", flow=Flow("call", bitvec.const(64, 64))),
                    sem(32, "exit", flow=Flow("return", bitvec.const(0, 64))),
                    sem(64, "return", flow=Flow("return", bitvec.zext(register(4), 64)))]
            with self.assertRaisesRegex(InferenceError, "return target not proved"): expanded(sems)

    def test_known_low_word_survives_unknown_high_word(self):
        g = Graph()
        low, high = g.node("const", 32, 32), g.input("high", 32)
        pair = g.concat((high, low))
        projection = g.extract(pair, 31, 0)
        result = call_graph.constants(g, [projection, pair])
        self.assertEqual(result[projection], frozenset([32]))
        self.assertIsNone(result[pair])

    def test_indirect_calls_and_contextless_returns_are_rejected(self):
        cases = [sem(0, "indirect call", flow=Flow("call", register(2, 2))),
                 sem(0, "unmatched return", flow=Flow("return", register(2, 2)))]
        for case in cases:
            with self.assertRaises(InferenceError): expanded([case])

    def test_recursive_expansion_stops_at_explicit_limit(self):
        sems = [sem(0, "recursive call", flow=Flow("call", bitvec.const(0, 64)))]
        with self.assertRaisesRegex(InferenceError, "limit exceeded"): call_graph.expand(sems, depth=3)

    def test_two_call_sites_have_separate_return_contexts(self):
        sems = [sem(0, "first return address", [(register(4), bitvec.const(32, 32))]),
                sem(16, "first call", flow=Flow("call", bitvec.const(96, 64))),
                sem(32, "second return address", [(register(4), bitvec.const(64, 32))]),
                sem(48, "second call", flow=Flow("call", bitvec.const(96, 64))),
                sem(64, "exit", flow=Flow("return", bitvec.const(0, 64))),
                sem(96, "return", flow=Flow("return", bitvec.zext(register(4), 64)))]
        g = expanded(sems)
        self.assertEqual({r["return_target"] for r in g.verified_returns}, {32, 64})
        self.assertEqual(len(g.verified_returns), 2)


@unittest.skipUnless(CORPUS.exists(), "set SHAPE_CORPUS to the handwritten cubin corpus")
class CorpusCallTests(unittest.TestCase):
    def test_math_helpers_have_verified_returns_and_typed_pointer_inputs(self):
        for name, continuation in (("KC_nrm2_simple", 0x340), ("KC_strsm_lower_8x8", 0xbc0)):
            with self.subTest(name=name):
                g = kernel(CORPUS / name / f"{name}_sm90.elf")
                self.assertEqual([r["return_target"] for r in g.verified_returns], [continuation])
                i = Inference(g).generate().solve()
                self.assertTrue(any(t.startswith("ptr64<global, f32") for t in i.report()["inputs"].values()))
                self.assertTrue(any(view["storage"] == "f32" and view["as"] == "i32" for view in i.report()["integer_views"]))


if __name__ == "__main__": unittest.main()
