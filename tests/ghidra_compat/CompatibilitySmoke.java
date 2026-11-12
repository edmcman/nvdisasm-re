import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;

public class CompatibilitySmoke extends GhidraScript {
    @Override
    public void run() throws Exception {
        if (!currentProgram.getLanguageID().toString().equals("SASS:LE:64:sm89"))
            throw new Exception("Unexpected language: " + currentProgram.getLanguageID());
        Function function = getGlobalFunctions("write_value").get(0);
        Instruction instruction = getInstructionAt(function.getEntryPoint());
        if (instruction == null || !instruction.getMnemonicString().equals("MOV"))
            throw new Exception("Expected MOV at kernel entry: " + instruction);
        if (function.getParameterCount() != 2 || function.getParameter(0).getLength() != 8 ||
                function.getParameter(1).getLength() != 4)
            throw new Exception("Kernel argument analyzer did not recover parameters");
        println("SASS_COMPATIBILITY_SMOKE_PASSED");
    }
}
