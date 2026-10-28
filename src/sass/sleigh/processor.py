"""Build the processor's Ghidra loader and analyzers against the selected installation."""
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from sass.paths import ROOT


def replace(source, target):
    """Publish a complete JAR so concurrent Ghidra launches never see a partial archive."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        shutil.copyfile(source, temporary)
        temporary.chmod(0o644)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def build(install, output):
    """Write lib/SASS.jar beside generated languages, and into the default module."""
    install, output = Path(install), Path(output)
    sources = sorted((ROOT / "processor/SASS/src/main/java").rglob("*.java"))
    properties = dict(line.split("=", 1) for line in
                      (install / "Ghidra/application.properties").read_text().splitlines()
                      if "=" in line and not line.startswith("#"))
    release = properties["application.java.min"]
    javac = str(Path(os.environ["JAVA_HOME"]) / "bin/javac") if "JAVA_HOME" in os.environ else "javac"
    jars = os.pathsep.join(str(p) for p in sorted((install / "Ghidra").rglob("*.jar")))
    target = output / "lib/SASS.jar"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        classes = Path(temporary) / "classes"
        classes.mkdir()
        subprocess.run([javac, "--release", release, "-proc:none", "-cp", jars, "-d",
                        str(classes), *map(str, sources)], check=True)
        jar = Path(temporary) / target.name
        with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as archive:
            # Stable timestamps make builds of the same sources byte-identical.
            for path in sorted(classes.rglob("*.class")):
                info = zipfile.ZipInfo(path.relative_to(classes).as_posix())
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
        replace(jar, target)
    if output.resolve() == (ROOT / "processor/SASS/data/languages").resolve():
        module_jar = ROOT / "processor/SASS/lib" / target.name
        replace(target, module_jar)
    return target
