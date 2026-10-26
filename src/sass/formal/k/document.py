"""Publish the executable Markdown definition as Markdown, LaTeX, and PDF.

Run: nice uv run python -m sass.formal.k.document
Requires Pandoc and, unless --tex-only is used, pdflatex with TeX Live's
recommended LaTeX packages. K is not required for publication.
"""
import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
import shutil
import subprocess

from sass import paths
from sass.formal import k
from sass.formal.k import example, render


def build(directory, *, pdf=True, prove_example=False):
    """Assemble the live literate sources; retain the .tex and compiler log."""
    required = ("pandoc", "pdflatex") if pdf else ("pandoc",)
    for tool in required:
        if not shutil.which(tool):
            raise RuntimeError(f"{tool} is required; see docs/semantics.md, Published definition")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    sources = [k.DEFINITION.with_name("reference.md"),
               k.DEFINITION.with_name("operational.md"), *k.SOURCES]
    digest = hashlib.sha256()
    sections = []
    rules = []
    for source in sources:
        content = source.read_bytes()
        digest.update(source.name.encode())
        digest.update(content)
        if source in k.SOURCES:
            section, extracted = render.publication(source)
            sections.append(section)
            rules.extend(extracted)
        else:
            sections.append(content.decode())
    worked = example.make().publication(directory / "example", prove=prove_example)
    for name in ("example.py", "execution.py", "worked-example.md", "render.py", "notation.py", "document.tex"):
        digest.update(name.encode())
        digest.update(k.DEFINITION.with_name(name).read_bytes())
    digest.update(worked.encode())
    sections.append(worked)
    (directory / "rules.json").write_text(
        json.dumps([asdict(rule) for rule in rules], indent=2) + "\n")
    markdown = directory / "sass-ir-semantics.md"
    markdown.write_text(
        '---\ntitle: "SASS IR: Executable Formal Semantics"\n'
        f'author: "nvdisasm-re · K {k.VERSION}"\ndate: ""\n'
        'fontsize: 10pt\ngeometry: margin=0.8in\n'
        'colorlinks: true\n---\n\n'
        f'Source fingerprint (SHA-256): `{digest.hexdigest()}`.\n\n'
        + "\n\n".join(sections), encoding="utf-8")
    tex = markdown.with_suffix(".tex")
    result = subprocess.run(
        ["nice", "pandoc", str(markdown), "--from", "markdown", "--to", "latex",
         "--standalone", "--toc", "--number-sections", "--listings",
         "--include-in-header", str(k.DEFINITION.with_name("document.tex")),
         "--output", str(tex)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Pandoc failed:\n{result.stderr}")
    if pdf:
        # The contents can change pagination; continue until references settle.
        for attempt in range(4):
            result = subprocess.run(
                ["nice", "pdflatex", "-no-shell-escape", "-interaction=nonstopmode",
                 "-halt-on-error", tex.name], cwd=directory, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(f"pdflatex failed; see {tex.with_suffix('.log')}:\n{result.stdout[-4000:]}")
            log = tex.with_suffix(".log").read_text()
            if attempt > 0 and "Rerun to get" not in log:
                break
        else:
            raise RuntimeError(f"LaTeX references did not settle; see {tex.with_suffix('.log')}")
    return tex.with_suffix(".pdf") if pdf else tex


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, default=paths.CACHE / "formal-doc")
    parser.add_argument("--tex-only", action="store_true", help="generate Markdown and LaTeX without compiling PDF")
    parser.add_argument("--prove-example", action="store_true",
                        help="check instruction and whole-execution reachability claims with K")
    args = parser.parse_args(argv)
    try:
        result = build(args.output_directory, pdf=not args.tex_only,
                       prove_example=args.prove_example)
    except RuntimeError as error:
        parser.exit(1, f"{error}\n")
    print(result)


if __name__ == "__main__":
    main()
