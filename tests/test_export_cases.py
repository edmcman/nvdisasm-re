"""The shared-case export: complete, decodable JSON lines, gzipped by path."""
import gzip, io, json, sys
from sass.decode import pydecode
from tests import export_cases
from tests.cases import emulator_cases

ARCH = "SM80"


def exported(families, generated):
    out = io.StringIO()
    counts = export_cases.export(out, ARCH, families=families, generated=generated)
    return counts, [json.loads(line) for line in out.getvalue().splitlines()]


def expected(families, generated):
    return [export_cases.record(ARCH, "exact", f, case) for f, cases in emulator_cases(ARCH, "exact", families, generated=generated).items() for case in cases]


def test_export_matches_cases():
    for families, generated in (("int_alu",), False), (("bmsk",), True):
        counts, records = exported(families, generated)
        assert records == expected(families, generated) and counts.total() == len(records)
        assert len({r["id"] for r in records}) == len(records)
        assert all(pydecode.decode(ARCH, bytes.fromhex(r["word"])).klass.name == r["class"] for r in records)


def test_gzip_output(tmp_path, monkeypatch):
    path = tmp_path / "cases.jsonl.gz"
    monkeypatch.setattr(sys, "argv", ["export_cases", ARCH, "--family", "int_simd", "-o", str(path)])
    export_cases.main()
    assert [json.loads(line) for line in gzip.open(path, "rt")] == expected(("int_simd",), False)
