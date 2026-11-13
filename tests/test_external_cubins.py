import os
from pathlib import Path

import pytest

from sass.decode import cubin
from sass.semantics import lifter


def test_external_cubins_lift():
    root = os.environ.get("SASS_CUBIN_CORPUS")
    if not root: pytest.skip("set SASS_CUBIN_CORPUS to a directory of cubins")
    files = sorted(Path(root).rglob("*.cubin"))
    assert files, f"no cubins in {root}"
    seen = set()
    failures = []
    for path in files:
        data = path.read_bytes()
        arch = cubin.elf_arch(data)
        for name, body in cubin.text_sections(data).items():
            for index, word in enumerate(cubin.words(body)):
                key = arch, word
                if key in seen: continue
                seen.add(key)
                try:
                    sem = lifter.lift(arch, word, index * 16)
                    if sem.status == "opaque": failures.append((path, name, index * 16, sem.text, sem.reason))
                except Exception as error:
                    failures.append((path, name, index * 16, word.hex(), str(error)))
    assert seen, f"no instructions in {root}"
    assert not failures, "\n".join(map(str, failures))
