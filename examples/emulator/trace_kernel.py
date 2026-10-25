"""Run a kernel on the native emulator and show its state: each step's instruction and the registers it
wrote (kernel.trace, for the first trace_threads threads), then every thread's output in memory."""
import struct
from pathlib import Path
from sass import launch
from sass.assemble import assemble_cubin
from sass.decode import cubin
from sass.emulate import kernel

IN, OUT = 0x1_0000_0000, 0x2_0000_0000
image = cubin.kernel_image(assemble_cubin((Path(__file__).parent / "times3.sass").read_text()), "times3")
inputs = [5, 0x7fffffff, 1000, 0xffffffff]
memory = [dict(space="ram", address=IN, hex=struct.pack(f"<{len(inputs)}I", *inputs).hex())]
request = launch.kernel_request(image, (IN, OUT), memory, threads=len(inputs), block=len(inputs), output=dict(address=OUT, size=4, stride=4))
request["kernel"].update(trace=True, trace_threads=2)

response = kernel.run_kernel(request, image["arch"])
for step in response["trace"]:
    writes = " ".join(f"{name}={value:#x}" for name, value in step["writes"].items())
    print(f"thread {step['thread']} {step['pc']:#06x} {step['text']:40} {writes}".rstrip())
for thread, (x, out) in enumerate(zip(inputs, response["outputs"])):
    print(f"thread {thread}: in {x:#010x} -> out {int.from_bytes(bytes.fromhex(out), 'little'):#010x}")
