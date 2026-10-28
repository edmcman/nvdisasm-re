"""Import and decompile a cubin with checkout languages, without installing the processor."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import pyghidra
from sass.ghidra_bridge import ghidra_decode


def main(ldefs, language_id, cubin, *options):
    from pyghidra.launcher import HeadlessPyGhidraLauncher
    from sass.paths import ROOT
    jar = ROOT / "processor/SASS/lib/SASS.jar"
    assert jar.exists(), "Build the SASS processor first"
    launcher = HeadlessPyGhidraLauncher(install_dir=ghidra_decode.GHIDRA)
    launcher.add_class_files(str(jar))
    launcher.start()
    from java.io import File
    from generic.jar import ResourceFile
    from ghidra.app.plugin.processors.sleigh import SleighLanguageProvider
    from ghidra.program.model.lang import LanguageProvider, LanguageID
    from ghidra.program.util import DefaultLanguageService
    from ghidra.app.util.importer import ProgramLoader
    from ghidra.app.cmd.disassemble import DisassembleCommand
    from ghidra.app.decompiler import DecompInterface
    from ghidra.program.model.address import AddressSet
    from ghidra.program.model.symbol import SourceType
    from ghidra.util.task import TaskMonitor

    # Give the real ELF loader the same private provider our standalone decoder uses.
    ctor = SleighLanguageProvider.class_.getDeclaredConstructor(ResourceFile.class_)
    ctor.setAccessible(True)
    provider = ctor.newInstance(ResourceFile(File(ldefs)))
    ctor = DefaultLanguageService.class_.getDeclaredConstructor(LanguageProvider.class_)
    ctor.setAccessible(True)
    service = ctor.newInstance(provider)
    field = DefaultLanguageService.class_.getDeclaredField("languageService")
    field.setAccessible(True)
    field.set(None, service)
    lang = service.getLanguage(LanguageID(language_id))
    loaded = (ProgramLoader.builder().source(File(cubin))
              .language(lang).compiler("default").load())
    decompiler = DecompInterface()
    try:
        program = loaded.getPrimaryDomainObject()
        for original, readable in (("SR_TID", "threadIdx"), ("SR_CTAID", "blockIdx")):
            for axis in "xyz":
                register = lang.getRegister(f"{original}.{axis.upper()}")
                assert register is not None
                assert register == lang.getRegister(f"{readable}_{axis}")
                assert register.getMinimumByteSize() == 4
        block = program.getMemory().getBlock("constant_bank_0")
        assert block is not None and not block.isInitialized()
        assert block.isRead() and not block.isWrite() and not block.isExecute()
        labels = {str(info.getLabel()): int(info.getAddress().getOffset())
                  for info in lang.getDefaultSymbols()}
        for name, offset in labels.items():
            address = program.getAddressFactory().getAddressSpace("cbank").getAddress(offset)
            assert str(program.getSymbolTable().getPrimarySymbol(address).getName()) == name
        # Import defines the byte extent before any analysis runs, independently
        # of the distinct parameter types later assigned to each function.
        bank = program.getAddressFactory().getAddressSpace("cbank")
        argument_base = bank.getAddress(labels["kernel_arguments"])
        region = program.getListing().getDataAt(argument_base)
        assert region is not None and region.getLength() == 4096
        assert program.getListing().getDataContaining(argument_base.add(0x10)) == region
        assert program.getListing().getDataContaining(argument_base.add(4095)) == region
        assert program.getListing().getDefinedDataAt(argument_base.add(4096)) is None
        code = program.getMemory().getBlock(".text.constant_labels")
        assert code is not None
        transaction = program.startTransaction("Disassemble test kernel")
        try:
            assert DisassembleCommand(code.getStart(), AddressSet(code.getStart(), code.getEnd()), True).applyTo(program)
            function = program.getFunctionManager().getFunctionAt(code.getStart())
            if function is None:
                function = program.getFunctionManager().createFunction(
                    "constant_labels", code.getStart(), AddressSet(code.getStart(), code.getEnd()), SourceType.USER_DEFINED)
            other = program.getMemory().getBlock(".text.other_layout")
            assert DisassembleCommand(other.getStart(), AddressSet(other.getStart(), other.getEnd()), True).applyTo(program)
        finally:
            program.endTransaction(transaction, True)
        # Renaming display registers must preserve old-name seeding and S2R
        # execution, including every thread/block coordinate.
        import jpype
        from ghidra.pcode.emu import PcodeEmulator
        from ghidra.pcode.exec import PcodeProgram, PcodeExecutorStatePiece
        reason = PcodeExecutorStatePiece.Reason.INSPECT
        coordinates = [instruction for instruction in program.getListing().getInstructions(
            AddressSet(code.getStart(), code.getEnd()), True)
            if str(instruction.getMnemonicString()) == "S2R"]
        assert len(coordinates) == 6
        for instruction in coordinates:
            original, readable, target, axis = next(
                (original, readable, target, axis)
                for original, readable, target in (("SR_TID", "threadIdx", "R4"),
                                                   ("SR_CTAID", "blockIdx", "R5"))
                for axis in "xyz" if f"{original}.{axis.upper()}" in str(instruction))
            emulator = PcodeEmulator(lang)
            thread = emulator.newThread()
            state = thread.getState()
            expected = 0x10203040 + "xyz".index(axis)
            register = lang.getRegister(f"{original}.{axis.upper()}")
            state.setVar(register, jpype.JArray(jpype.JByte)(
                expected.to_bytes(4, "little")))
            pcode = PcodeProgram.fromInstruction(instruction, False)
            thread.getExecutor().execute(pcode, thread.getUseropLibrary())
            value = int.from_bytes(bytes(state.getVar(lang.getRegister(target), reason)), "little")
            assert value == expected
            assert int.from_bytes(bytes(state.getVar(
                lang.getRegister(f"{readable}_{axis}"), reason)), "little") == expected
        extra = {}
        from ghidra.app.plugin.core.analysis import AutoAnalysisManager
        from ghidra.framework.options import OptionType
        from ghidra.program.model.listing import Program
        manager = AutoAnalysisManager.getAnalysisManager(program)
        if "--arguments" in options:
            analyzer = manager.getAnalyzer("SASS Kernel Arguments")
            assert analyzer is not None, "Ghidra did not discover the installed analyzer"
            assert analyzer.getDefaultEnablement(program)
        # Ghidra's ordinary Reference analyzer supplies operand references. It
        # must display interior offsets even when the SASS analyzer is disabled.
        transaction = program.startTransaction("Select analysis")
        try:
            analysis = program.getOptions(Program.ANALYSIS_PROPERTIES)
            enabled = {"Reference"}
            if "--arguments" in options:
                enabled.add("SASS Kernel Arguments")
            for name in analysis.getOptionNames():
                if analysis.getType(name) == OptionType.BOOLEAN_TYPE and "." not in str(name):
                    analysis.setBoolean(name, str(name) in enabled)
            manager.initializeOptions()
        finally:
            program.endTransaction(transaction, True)
        transaction = program.startTransaction("Automatic kernel analysis")
        try:
            manager.reAnalyzeAll(program.getMemory())
            manager.startAnalysis(TaskMonitor.DUMMY)
        finally:
            program.endTransaction(transaction, True)
        from ghidra.program.model.listing import CodeUnitFormat, CodeUnitFormatOptions
        formatter = CodeUnitFormat(CodeUnitFormatOptions.ShowBlockName.NEVER,
                                   CodeUnitFormatOptions.ShowNamespace.NEVER)
        offset_address = argument_base.add(0x10)
        references = list(program.getReferenceManager().getReferencesTo(offset_address))
        assert references, "Reference analysis did not find the argument offset"
        operand_labels = [str(formatter.getOperandRepresentationString(
            program.getListing().getInstructionAt(ref.getFromAddress()), ref.getOperandIndex()))
            for ref in references]
        assert any("kernel_arguments[16]" in text for text in operand_labels), operand_labels
        extra["offset_operands"] = operand_labels
        if "--arguments" in options:
            extra["parameters"] = [p.getLength() for p in function.getParameters()]
            assert extra["parameters"] == [8]
            other_function = program.getFunctionManager().getFunctionAt(other.getStart())
            extra["other_parameters"] = [p.getLength() for p in other_function.getParameters()]
            assert extra["other_parameters"] == [4, 8]
            # One bank address is an 8-byte argument in one kernel and a 4-byte
            # argument in another. Function-local custom storage keeps both valid.
            first_address = function.getParameter(0).getVariableStorage().getVarnodes()[0].getAddress()
            other_address = other_function.getParameter(0).getVariableStorage().getVarnodes()[0].getAddress()
            assert first_address == other_address
        assert decompiler.openProgram(program)
        result = decompiler.decompileFunction(function, 30, TaskMonitor.DUMMY)
        assert result.decompileCompleted(), str(result.getErrorMessage())
        text = str(result.getDecompiledFunction().getC())
        assert "blockDim_x" in text, text
        for coordinate in ("threadIdx", "blockIdx"):
            for axis in "xyz":
                assert f"{coordinate}_{axis}" in text, text
                assert f"in_{coordinate}_{axis}" not in text, text
        assert ("arg_0" if "--arguments" in options else "kernel_arguments") in text, text
        if str(lang.getLanguageID()).endswith(("sm90", "sm90_readable", "sm100", "sm100_readable")):
            assert "default_global_descriptor" in text, text
        if "--arguments" in options:
            result = decompiler.decompileFunction(other_function, 30, TaskMonitor.DUMMY)
            assert result.decompileCompleted(), str(result.getErrorMessage())
            other_text = str(result.getDecompiledFunction().getC())
            assert "arg_0" in other_text and "arg_1" in other_text, other_text
            assert "kernel_arguments" not in other_text, other_text
            extra["other_decompiled"] = other_text
            # A user-edited signature survives automatic reanalysis.
            transaction = program.startTransaction("Customize signature")
            try:
                function.getParameter(0).setName("custom_output", SourceType.USER_DEFINED)
                function.setSignatureSource(SourceType.USER_DEFINED)
            finally:
                program.endTransaction(transaction, True)
            transaction = program.startTransaction("Automatic kernel reanalysis")
            try:
                manager.reAnalyzeAll(program.getMemory())
                manager.startAnalysis(TaskMonitor.DUMMY)
            finally:
                program.endTransaction(transaction, True)
            assert str(function.getParameter(0).getName()) == "custom_output"
        print(json.dumps(dict(labels=labels, decompiled=text, **extra)))
    finally:
        decompiler.dispose()
        loaded.close()


if __name__ == "__main__":
    main(*sys.argv[1:])
