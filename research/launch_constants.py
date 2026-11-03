import re
import subprocess
import tempfile
from pathlib import Path
from sass import launch
from sass.decode import cubin, pydecode
from sass.paths import NVCC


def probe(arch):
    fields = [(f"{name}_{axis}", f"{name}Dim.{axis}", offset + i * 4)
              for name, offset in (("block", launch.LAUNCH_LAYOUT[arch][0]), ("grid", launch.GRID_LAYOUT[arch]))
              for i, axis in enumerate("xyz")]
    source = "\n".join(f'extern "C" __global__ void {name}(unsigned *out) {{ out[0] = {value}; }}'
                       for name, value, _ in fields)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory); (path / "probe.cu").write_text(source)
        target = "SM110" if arch == "SM101" else arch
        subprocess.run([NVCC, "--allow-unsupported-compiler", "-Wno-deprecated-gpu-targets",
                        "-cubin", f"-arch=sm_{target[2:]}", str(path / "probe.cu"), "-o", str(path / "probe.cubin")], check=True)
        image = (path / "probe.cubin").read_bytes()
        for name, _, expected in fields:
            kernel = cubin.kernel_image(image, name)
            text = "\n".join(pydecode.decode(arch, kernel["code"][i:i + 16]).text for i in range(0, len(kernel["code"]), 16))
            offsets = {int(v, 16) for v in re.findall(r"c\[0x0\]\[(0x[0-9a-f]+)\]", text.replace("[RZ]", "[0x0]"))}
            assert expected in offsets, (arch, name, expected, text)
        print(arch, " ".join(f"{name}={offset:#x}" for name, _, offset in fields), flush=True)


if __name__ == "__main__":
    for arch in launch.LAUNCH_LAYOUT: probe(arch)
