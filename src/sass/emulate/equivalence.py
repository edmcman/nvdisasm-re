"""Equivalence testing: two kernels (`sass.decode.cubin.kernel_image`s, for example a cubin and the
SASS a translation of it compiles to) run on the same launches in the native emulator, compared
thread by thread. A failure is an outcome too: both sides failing alike is no difference."""
from dataclasses import dataclass
from sass import launch
from sass.emulate import kernel


@dataclass(frozen=True)
class Launch:
    """`launch.kernel_request`'s inputs: parameter values, memory regions [{space, address, hex}],
    the output layout {address, size, stride}, and the grid."""
    arguments: tuple
    memory: tuple
    output: dict
    threads: int
    block: int = 32
    cooperative: bool = False
    context: dict | None = None


@dataclass(frozen=True)
class Difference:
    launch: int
    thread: int | None   # None: one side failed the whole launch
    a: str               # an output's hex, or the error
    b: str


def response(image, run, fp):
    request = launch.kernel_request(image, run.arguments, list(run.memory), run.threads, run.block, run.output, run.cooperative, run.context)
    return kernel.run_kernel(request, image["arch"], fp)


def differences(a, b, launches, fp="exact", same=str.__eq__):
    """The Differences between kernel images a and b over `launches`; `same(x, y)` compares two
    threads' output hex (default: bit for bit)."""
    for i, run in enumerate(launches):
        x, y = response(a, run, fp), response(b, run, fp)
        if "error" in x or "error" in y:
            if x.get("error") != y.get("error"): yield Difference(i, None, x.get("error", "completed"), y.get("error", "completed"))
            continue
        for t, (p, q) in enumerate(zip(x["outputs"], y["outputs"])):
            if not same(p, q): yield Difference(i, t, p, q)


def equivalent(a, b, launches, fp="exact", same=str.__eq__):
    """Whether kernel images a and b agree on every launch."""
    return next(differences(a, b, launches, fp, same), None) is None
