"""Headless integration checks against the actual filesystem service and cubin importer."""
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from sass.paths import ROOT
from sass.ghidra_bridge import ghidra_decode


def main(config_path):
    import jpype
    from pyghidra.launcher import HeadlessPyGhidraLauncher
    config = json.loads(Path(config_path).read_text())
    jar = ROOT / "processor/SASS/lib/SASS.jar"

    class WorktreeLauncher(HeadlessPyGhidraLauncher):
        def _pre_launch_init(self):
            # Use Ghidra's loader (not its parent JVM loader) before installed modules
            # are added. add_class_files appends too late to override an installed SASS.
            from java.lang import ClassLoader
            ClassLoader.getSystemClassLoader().addPath(str(jar))
            super()._pre_launch_init()

    launcher = WorktreeLauncher(install_dir=ghidra_decode.GHIDRA)
    launcher.start()
    from java.io import File
    from ghidra.formats.gfilesystem import FileSystemService
    from ghidra.formats.gfilesystem.factory import FileSystemFactoryMgr
    from ghidra.util.task import TaskMonitor, TaskMonitorAdapter
    from ghidra.util.exception import CancelledException
    from ghidra.app.util.bin import ByteArrayProvider
    Parser = jpype.JClass("sass.SassEngineParser")
    Factory = jpype.JClass("sass.SassEngineFileSystemFactory")
    CubinLoader = jpype.JClass("sass.SassCubinLoader")
    assert Path(str(CubinLoader.class_.getProtectionDomain().getCodeSource().getLocation()
                    .toURI().getPath())).resolve() == jar.resolve()
    service = FileSystemService.getInstance()
    assert "TensorRT CUDA Kernel Browser" in list(FileSystemFactoryMgr.getInstance().getAllFilesystemNames())
    monitor = TaskMonitor.DUMMY
    destination = Path(config["directory"])

    def mount(path):
        fsrl = service.getLocalFSRL(File(str(path)))
        ref = service.probeFileForFilesystem(fsrl, monitor, None)
        assert ref is not None, f"Filesystem not discovered for {path}"
        assert str(ref.getFilesystem().getType()) == "sassengine"
        return ref

    def read(fs, path):
        file = fs.lookup(path)
        assert file is not None, path
        provider = fs.getByteProvider(file, monitor)
        try:
            assert str(provider.getFSRL().getPath()) == str(file.getPath())
            return bytes(provider.readBytes(0, provider.length()))
        finally:
            provider.close()

    def configure_languages():
        from generic.jar import ResourceFile
        from ghidra.app.plugin.processors.sleigh import SleighLanguageProvider
        from ghidra.program.model.lang import LanguageProvider
        from ghidra.program.util import DefaultLanguageService
        from ghidra.app.util.opinion import QueryOpinionService
        ctor = SleighLanguageProvider.class_.getDeclaredConstructor(ResourceFile.class_)
        ctor.setAccessible(True)
        language_provider = ctor.newInstance(ResourceFile(File(config["ldefs"])))
        ctor = DefaultLanguageService.class_.getDeclaredConstructor(LanguageProvider.class_)
        ctor.setAccessible(True)
        language_service = ctor.newInstance(language_provider)
        field = DefaultLanguageService.class_.getDeclaredField("languageService")
        field.setAccessible(True)
        field.set(None, language_service)

        # The private language provider does not install its .opinion file in
        # Ghidra's application resources. Load the checkout's generated mapping
        # with Ghidra's own parser, after replacing the language service. Remove
        # any installed CUDA opinions first so a local installation cannot mask
        # a missing or broken checkout mapping.
        initialize = QueryOpinionService.class_.getDeclaredMethod("initialize")
        initialize.setAccessible(True)
        initialize.invoke(None)
        database = QueryOpinionService.class_.getDeclaredField("DATABASE")
        database.setAccessible(True)
        elf_opinions = database.get(None).get("Executable and Linking Format (ELF)")
        if elf_opinions is not None:
            elf_opinions.remove("190")
        assert not QueryOpinionService.query("Executable and Linking Format (ELF)", "190",
                                             str(0x6005904))
        parse = QueryOpinionService.class_.getDeclaredMethod("parseFile", ResourceFile.class_)
        parse.setAccessible(True)
        parse.invoke(None, ResourceFile(File(str(Path(config["ldefs"]).with_name("sass.opinion")))))

    def import_module(fs, module):
        from sass.decode import cubin
        from ghidra.app.util.importer import ProgramLoader
        from ghidra.app.plugin.core.analysis import AutoAnalysisManager
        from ghidra.framework.options import OptionType
        from ghidra.program.model.listing import Program
        from ghidra.app.util.opinion import LoaderService
        data = read(fs, module["path"])
        provider = fs.getByteProvider(fs.lookup(module["path"]), monitor)
        try:
            supported = LoaderService.getSupportedLoadSpecs(provider, LoaderService.ACCEPT_ALL, monitor)
            assert str(supported.firstKey().getName()) == "CUDA Cubin", str(supported)
            cuda_specs = supported.get(supported.firstKey())
            assert any(spec.isPreferred() and spec.getLanguageCompilerSpec() is not None and
                       str(spec.getLanguageCompilerSpec().languageID) ==
                       f"SASS:LE:64:{cubin.elf_arch(data).lower()}" for spec in cuda_specs), \
                f"No preferred CUDA load specification for {module['path']}: {list(cuda_specs)}"
            # Let ELF opinions select the architecture, as the browser's Import action does.
            loaded = ProgramLoader.builder().source(provider).load()
            try:
                program = loaded.getPrimaryDomainObject()
                assert str(program.getLanguageID()) == f"SASS:LE:64:{cubin.elf_arch(data).lower()}"
                assert str(program.getExecutableFormat()) == "Executable and Linking Format (ELF)"
                if data[8] < 8:
                    assert int(program.getImageBase().getOffset()) >= max(0x100000, len(data))
                images = [cubin.kernel_image(data, name) for name in module["elf"]["kernels"]]
                bank = program.getAddressFactory().getAddressSpace("cbank")
                assert str(program.getSymbolTable().getPrimarySymbol(
                    bank.getAddress(0)).getName()) == "blockDim_x"
                for image in images:
                    code = program.getMemory().getBlock(".text." + image["name"])
                    assert code is not None and code.isExecute()
                    assert list(program.getSymbolTable().getSymbols(image["name"]))
                    address = bank.getAddress(image["parameter_base"])
                    assert program.getListing().getDataAt(address).getLength() == 4096
                # Constant sections survive import with their exact bytes.
                for name, body in cubin.sections(data).items():
                    if name.startswith(".nv.constant") and body:
                        block = program.getMemory().getBlock(name)
                        assert block is not None, name
                        memory = jpype.JArray(jpype.JByte)(len(body))
                        assert program.getMemory().getBytes(block.getStart(), memory) == len(body)
                        assert bytes(memory) == body
                manager = AutoAnalysisManager.getAnalysisManager(program)
                assert manager.getAnalyzer("SASS Kernel Arguments") is not None
                transaction = program.startTransaction("Analyze imported engine kernels")
                try:
                    options = program.getOptions(Program.ANALYSIS_PROPERTIES)
                    for option in options.getOptionNames():
                        if options.getType(option) == OptionType.BOOLEAN_TYPE and "." not in str(option):
                            options.setBoolean(option, str(option) == "SASS Kernel Arguments")
                    manager.initializeOptions()
                    manager.reAnalyzeAll(program.getMemory())
                    manager.startAnalysis(monitor)
                finally:
                    program.endTransaction(transaction, True)
                for image in images:
                    code = program.getMemory().getBlock(".text." + image["name"])
                    function = program.getFunctionManager().getFunctionAt(code.getStart())
                    assert function is not None, image["name"]
                    expected = image["parameters"]
                    assert [parameter.getLength() for parameter in function.getParameters()] == \
                        [parameter["size"] for parameter in expected]
                    assert [str(parameter.getName()) for parameter in function.getParameters()] == \
                        [f"arg_{i}" for i in range(len(expected))]
                    for i, parameter in enumerate(expected):
                        address = function.getParameter(i).getVariableStorage().getVarnodes()[0].getAddress()
                        assert address.getAddressSpace() == bank
                        assert int(address.getOffset()) == image["parameter_base"] + parameter["offset"]
                return dict(architecture=module["elf"]["architecture"],
                            kernels=len(images), parameters=[len(image["parameters"]) for image in images])
            finally:
                loaded.close()
        finally:
            provider.close()

    if "sample" in config:
        configure_languages()
        ref = mount(config["sample"])
        try:
            fs = ref.getFilesystem()
            manifest_bytes = read(fs, "manifest.json")
            (destination / "manifest.json").write_bytes(manifest_bytes)
            manifest = json.loads(manifest_bytes)
            for module in manifest["modules"]:
                path = destination / module["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(read(fs, module["path"]))
            (destination / "metadata.json").write_bytes(read(fs, "metadata.json"))
            imports = [import_module(fs, next(module for module in manifest["modules"]
                       if module["source"]["compression"] == kind)) for kind in ("none", "lz4")]
            (destination / "imports.json").write_text(json.dumps(imports, indent=2) + "\n")
            print(json.dumps(dict(occurrences=len(manifest["occurrences"]),
                                  unique=len(manifest["modules"]), imports=imports)))
        finally:
            ref.close()
        return

    expected_modules = {sha: (destination / f"{sha}.cubin").read_bytes()
                        for sha in config["modules"]}
    for fixture in config["fixtures"]:
        path = Path(fixture["file"])
        provider = ByteArrayProvider(jpype.JArray(jpype.JByte)(path.read_bytes()))
        try:
            assert Factory().probe(provider, service, monitor) == fixture["accepted"], fixture["name"]
        finally:
            provider.close()
        if not fixture["accepted"]:
            continue
        ref = mount(path)
        try:
            fs = ref.getFilesystem()
            manifest = json.loads(read(fs, "manifest.json"))
            assert len(manifest["occurrences"]) == fixture["occurrences"], (fixture, manifest)
            assert len(manifest["modules"]) == fixture["unique"], (fixture, manifest)
            if fixture["diagnostic"]:
                assert any(fixture["diagnostic"] in item["message"]
                           for item in manifest["diagnostics"]), (fixture, manifest)
            else:
                assert not manifest["diagnostics"], (fixture, manifest)
            assert (fs.lookup("metadata.json") is not None) == fixture["wrapped"]
            for module in manifest["modules"]:
                data = read(fs, module["path"])
                assert data == expected_modules[module["sha256"]], (fixture, module)
                assert hashlib.sha256(data).hexdigest() == module["sha256"]
                assert read(fs, module["path"]) == data  # second cache open
                assert str(fs.lookup(module["path"]).getParentFile().getName()) == "SM89"
                assert module["elf"]["kernels"]
            for occurrence in manifest["occurrences"]:
                offset, size = occurrence["offset"], occurrence["storedSize"]
                assert offset >= 24 and size > 0 and offset + size <= path.stat().st_size
                assert occurrence["cubinSize"] == len(expected_modules[occurrence["sha256"]])
                if occurrence["compression"] == "none":
                    assert path.read_bytes()[offset:offset + occurrence["cubinSize"]] == \
                        expected_modules[occurrence["sha256"]]
        finally:
            ref.close()

    # The factory propagates cancellation. Probe must leave the caller's provider open.
    cancelled = TaskMonitorAdapter(True)
    cancelled.cancel()
    provider = ByteArrayProvider(jpype.JArray(jpype.JByte)(
        Path(config["fixtures"][0]["file"]).read_bytes()))
    try:
        try:
            Factory().probe(provider, service, cancelled)
            raise AssertionError("Cancelled probe succeeded")
        except CancelledException:
            pass
        assert provider.readByte(0) == ord("f")
        try:
            Parser.scan(provider, None, cancelled)
            raise AssertionError("Cancelled scan succeeded")
        except CancelledException:
            pass
    finally:
        provider.close()

    class CancelDuringScan:
        def __init__(self):
            self.checks = 0

        def checkCancelled(self):
            self.checks += 1
            if self.checks == 10:
                raise CancelledException()

        def __getattr__(self, name):
            return getattr(monitor, name)

    halfway = CancelDuringScan()
    provider = ByteArrayProvider(jpype.JArray(jpype.JByte)(
        Path(config["fixtures"][0]["file"]).read_bytes()))
    try:
        try:
            Parser.scan(provider, None, jpype.JProxy(TaskMonitor, inst=halfway))
            raise AssertionError("Cancellation during ELF validation was ignored")
        except CancelledException:
            assert halfway.checks == 10
    finally:
        provider.close()

    configure_languages()
    imported = 0
    for name in ("raw", "compressed", "legacy", "legacy_compressed"):
        fixture = next(item for item in config["fixtures"] if item["name"] == name)
        ref = mount(fixture["file"])
        try:
            fs = ref.getFilesystem()
            module = json.loads(read(fs, "manifest.json"))["modules"][0]
            report = import_module(fs, module)
            assert report["parameters"] == [2]
            imported += 1
        finally:
            ref.close()
    print(json.dumps(dict(fixtures=len(config["fixtures"]), imported=imported, cancelled=True)))


if __name__ == "__main__":
    main(sys.argv[1])
