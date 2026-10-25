"""Launch a whole kernel on the native emulator: out[i] = 3 * in[i] + 1 over 4096 threads, and the
same request on the Python engine, which answers it identically (slower)."""
import random, struct
from pathlib import Path
from sass import launch
from sass.assemble import assemble_cubin
from sass.decode import cubin
from sass.emulate import kernel, reference

IN, OUT, N = 0x1_0000_0000, 0x2_0000_0000, 4096   # buffers in the emulated global memory
data = assemble_cubin((Path(__file__).parent / "times3.sass").read_text())
image = cubin.kernel_image(data, "times3")       # code, constant banks and parameter layout

inputs = [random.Random(i).getrandbits(32) for i in range(N)]
memory = [dict(space="ram", address=IN, hex=struct.pack(f"<{N}I", *inputs).hex())]
request = launch.kernel_request(image, (IN, OUT), memory, threads=N, block=128, output=dict(address=OUT, size=4, stride=4))

response = kernel.run_kernel(request, image["arch"])   # compiles the kernel's C++ on first use
outputs = [int.from_bytes(bytes.fromhex(h), "little") for h in response["outputs"]]
print(f"{response['threads']} threads, {response['steps']} steps; out[1] = {outputs[1]:#x} = 3 * {inputs[1]:#x} + 1:",
      outputs == [(3 * x + 1) & 0xffffffff for x in inputs])
small = dict(request, kernel=dict(request["kernel"], threads=128))
print("the Python engine agrees:", reference.run_kernel(small, image["arch"]) == kernel.run_kernel(small, image["arch"]))
