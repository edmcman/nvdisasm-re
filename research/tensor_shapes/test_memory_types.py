import unittest

from infer_types import Graph, Inference, kernel
from memory_types import MemoryTypes, footprint, infer
from access_relations import Normal, PrecisionLimit
from type_system import InferenceError, floating, integer
from test_infer_types import CORPUS


class MemoryTypeTests(unittest.TestCase):
    def test_shared_cell_connects_global_load_to_float_consumer(self):
        g = Graph()
        p = g.input("p", 64)
        value = g.node("load", 32, "ram", p)
        address = g.node("const", 64, 1024)
        g.accesses.append(("write", "shared", address, value, None, "write"))
        loaded = g.node("load", 32, "shared", address)
        g.node("fadd", 32, loaded, g.node("const", 32, 0), "RN")
        i, memory = infer(g)
        self.assertEqual(i.solver.resolve(i.types[p]).args[1], floating(32))
        self.assertEqual(memory.report()["cell_types"], {"shared:0x400": "f32"})
        self.assertFalse(memory.report()["initialization_proven"])

    def test_distinct_shared_cells_retain_distinct_types(self):
        g = Graph()
        a, b = g.node("const", 64, 1024), g.node("const", 64, 2048)
        x, y = g.node("load", 32, "shared", a), g.node("load", 32, "shared", b)
        g.node("fadd", 32, x, g.node("const", 32, 0), "RN")
        g.node("shl", 32, y, g.node("const", 32, 1))
        i, memory = infer(g)
        self.assertEqual(i.solver.resolve(i.types[x]), floating(32))
        self.assertEqual(i.solver.resolve(i.types[y]), integer(32))
        self.assertEqual(set(memory.report()["cell_types"].values()), {"f32", "i32"})

    def test_conflicting_cell_schemas_are_rejected(self):
        g = Graph()
        a = g.node("const", 64, 1024)
        x = g.node("load", 32, "shared", a, origin="float")
        y = g.node("load", 32, "shared", a, origin="integer")
        g.node("fadd", 32, x, g.node("const", 32, 0), "RN")
        g.node("shl", 32, y, g.node("const", 32, 1))
        i = Inference(g).generate()
        i.solver.constrain(i.types[y], integer(32), "fixed integer cell schema")
        MemoryTypes(i).generate()
        with self.assertRaisesRegex(InferenceError, "shared cell"): i.solve()

    def test_wide_shared_load_has_independently_typed_lanes(self):
        g = Graph()
        a = g.node("const", 64, 1024)
        vector = g.node("load", 128, "shared", a)
        floats = [g.extract(vector, k * 32 + 31, k * 32) for k in range(4)]
        for value in floats: g.node("fadd", 32, value, g.node("const", 32, 0), "RN")
        i, memory = infer(g)
        self.assertEqual(set(memory.report()["cell_types"].values()), {"f32"})
        self.assertEqual(len(memory.cells), 4)

    def test_missing_thread_range_stays_residual(self):
        g = Graph()
        thread = g.input("SR33", 32)
        address = g.node("zext", 64, g.node("mul", 32, thread, g.node("const", 32, 4)))
        g.node("load", 32, "shared", address)
        _, memory = infer(g)
        self.assertEqual(len(memory.residuals), 1)
        self.assertEqual(memory.cells, {})

    def test_enumerated_footprint_is_complete_and_bounded(self):
        g = Graph()
        thread = g.input("SR33", 32)
        expression = g.node("add", 32, g.node("const", 32, 1024), g.node("mul", 32, thread, g.node("const", 32, 4)))
        term = Normal(g)(expression)
        self.assertEqual(footprint(term, {"SR33:32": [0, 15]}), set(range(1024, 1088, 4)))
        with self.assertRaises(PrecisionLimit): footprint(term, {"SR33:32": [0, 100000]})


@unittest.skipUnless(CORPUS.exists(), "set SHAPE_CORPUS to the handwritten cubin corpus")
class CorpusMemoryTests(unittest.TestCase):
    def test_tiled_matrix_inputs_infer_float_through_shared_memory(self):
        path = CORPUS / "KC_sgemm_tiled_smem" / "KC_sgemm_tiled_smem_sm90.elf"
        ranges = {"SR33:32": [0, 15], "SR34:32": [0, 15], "SR136:32": [0, 0]}
        inference, memory = infer(kernel(path), ranges)
        for name in ("cb210", "cb218", "cb220"):
            p = inference.solver.resolve(inference.types[inference.graph.inputs[name, 64]])
            self.assertEqual(p.args[1], floating(32))
        self.assertEqual(len(memory.cells), 512)
        self.assertEqual(memory.residuals, [])
        self.assertEqual(set(memory.report()["cell_types"].values()), {"f32"})

    def test_reduction_output_infers_float_through_shared_memory(self):
        path = CORPUS / "KC_nrm2_unrolled" / "KC_nrm2_unrolled_sm90.elf"
        inference, memory = infer(kernel(path), {"SR33:32": [0, 127], "SR136:32": [0, 0]})
        p = inference.solver.resolve(inference.types[inference.graph.inputs["cb218", 64]])
        self.assertEqual(p.args[1], floating(32))
        self.assertEqual(memory.residuals, [])


if __name__ == "__main__": unittest.main()
