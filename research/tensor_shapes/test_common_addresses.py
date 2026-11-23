import unittest
from dataclasses import replace

from access_relations import Atom, signed
from common_constraints import CommonConstraints
from infer_types import kernel
import memory_types
from sass.decode import cubin
from sass.semantics import bitvec, evaluate, lifter
from test_common_shapes import corpus
from test_infer_types import CORPUS


def evaluate_row(row, analysis, state, phis):
    def atom_value(atom):
        match atom:
            case Atom('word', _, (source,)):
                environment = {(name, width): state.load('cbank', int(name[2:], 16), width) if name.startswith('cb')
                               else state.read('SR', int(name[2:]), 1) for name, width in memory_types.inputs(source)}
                bits = memory_types.evaluate(source, environment)
                return signed(bits, source.width) if source.width > 1 else bits
            case Atom('input', width, (name,)):
                return state.load('cbank', int(name[2:], 16), width)
            case Atom('iteration'): return phis[atom]
            case Atom('variable', _, (index, hi, lo)):
                bits = phis[index] >> lo & ((1 << (hi - lo + 1)) - 1)
                return signed(bits, hi - lo + 1) if hi - lo < 32 and hi != lo else bits
            case _: raise AssertionError(atom)
    result = 0
    for factors, coefficient in row.items():
        value = coefficient
        for factor in factors: value *= atom_value(factor)
        result += value
    return int(result) % (1 << 64)


@unittest.skipUnless(CORPUS.exists(), 'set SHAPE_CORPUS to the handwritten cubin corpus')
class CommonAddressTests(unittest.TestCase):
    def test_all_naive_gemm_addresses_against_lifted_sass(self):
        analysis = corpus('KC_sgemm_naive', 'kc_sgemm_naive').analysis
        path = CORPUS / 'KC_sgemm_naive' / 'KC_sgemm_naive_sm90.elf'
        image = cubin.kernel_image(path.read_bytes(), 'kc_sgemm_naive')
        sems = [lifter.lift(image['arch'], word, k * 16, fp='readable') for k, word in enumerate(cubin.words(image['code']))]
        equations = {a['origin']: analysis.states[a['block']].reduce(a['expression']) for a in analysis.accesses}
        sites, count = set(), 0
        for size in (1, 5):
            for reduction in (1, 3, 5, 9, 17, 33):
                state, phis, pc, previous_block = evaluate.State(), {}, 0, None
                for register, value in ((33, min(2, size - 1)), (34, 1), (37, 0), (38, 0)):
                    state.write('SR', register, 1, value)
                for offset, width, value in ((0, 32, 8), (4, 32, 8), (0x210, 64, 0x100000),
                                             (0x218, 64, 0x200000), (0x220, 64, 0x300000),
                                             (0x230, 32, size), (0x234, 32, 4), (0x238, 32, reduction)):
                    state.store('cbank', offset, width, value)
                for _ in range(2000):
                    for header, counter in analysis.loop_counts.items():
                        if header == pc:
                            phis[counter] = phis.get(counter, -1) + 1 if header in analysis.dominators.get(previous_block, ()) else 0
                    for phi, header in analysis.graph.phi_blocks.items():
                        if header == pc: phis[phi.index] = analysis.graph.read_phi(phi, state.read)
                    sem = sems[pc // 16]
                    active = sem.guard is None or evaluate.evaluate(sem.guard, state) != 0
                    if active and sem.flow and sem.flow.kind == 'return': break
                    origin = f'{sem.addr:#x}: {sem.text}'
                    if active and origin in equations:
                        addresses = [load.args[1] for _, value in sem.writes for load in bitvec.leaves(value, 'load') if load.args[0] == 'ram']
                        addresses.extend(store.address for store in sem.stores if store.space == 'ram')
                        self.assertEqual(len(addresses), 1)
                        self.assertEqual(evaluate_row(equations[origin], analysis, state, phis),
                                         evaluate.evaluate(addresses[0], state), (size, reduction, origin))
                        sites.add(origin); count += 1
                    evaluate.apply(replace(sem, effects=[]), state)
                    previous_block = analysis.graph.source_blocks[origin]
                    pc = state.pc
                else: self.fail('concrete execution did not terminate')
        self.assertEqual(sites, set(equations))
        self.assertGreater(count, 200)


if __name__ == '__main__': unittest.main()
