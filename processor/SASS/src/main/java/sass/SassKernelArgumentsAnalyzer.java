package sass;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.TreeMap;

import ghidra.app.cmd.function.CreateFunctionCmd;
import ghidra.app.services.AbstractAnalyzer;
import ghidra.app.services.AnalysisPriority;
import ghidra.app.services.AnalyzerType;
import ghidra.app.util.importer.MessageLog;
import ghidra.program.model.address.AddressSetView;
import ghidra.program.model.data.Undefined;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.ParameterImpl;
import ghidra.program.model.listing.Program;
import ghidra.program.model.listing.VariableStorage;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.SourceType;
import ghidra.util.exception.CancelledException;
import ghidra.util.task.TaskMonitor;

/** CUDA kernel parameters are function-local variables stored in constant bank zero. */
public class SassKernelArgumentsAnalyzer extends AbstractAnalyzer {
    public SassKernelArgumentsAnalyzer() {
        super("SASS Kernel Arguments",
            "Read CUDA .nv.info parameter offsets and sizes into kernel function signatures.",
            AnalyzerType.BYTE_ANALYZER);
        setPriority(AnalysisPriority.FUNCTION_ANALYSIS.after());
        setDefaultEnablement(true);
        setSupportsOneTimeAnalysis();
    }

    @Override
    public boolean canAnalyze(Program program) {
        return "SASS".equals(program.getLanguage().getProcessor().toString()) &&
            program.getAddressFactory().getAddressSpace("cbank") != null;
    }

    private record Parameter(int offset, int size) {}
    private record Layout(int base, int size, TreeMap<Integer, Parameter> parameters) {}

    private static Layout layout(Program program, MemoryBlock block) throws Exception {
        if (!block.isInitialized() || block.getSize() > Integer.MAX_VALUE) {
            throw new IllegalArgumentException("Unreadable CUDA parameter metadata");
        }
        byte[] bytes = new byte[(int) block.getSize()];
        if (program.getMemory().getBytes(block.getStart(), bytes) != bytes.length) {
            throw new IllegalArgumentException("Truncated CUDA parameter metadata");
        }
        ByteBuffer data = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        int base = -1;
        int size = -1;
        var parameters = new TreeMap<Integer, Parameter>();
        while (data.hasRemaining()) {
            if (data.remaining() < 4) {
                throw new IllegalArgumentException("Truncated CUDA attribute header");
            }
            int format = Byte.toUnsignedInt(data.get());
            int attribute = Byte.toUnsignedInt(data.get());
            int value = Short.toUnsignedInt(data.getShort());
            if (format != 4) {
                if (format < 1 || format > 3) {
                    throw new IllegalArgumentException("Unknown CUDA attribute format");
                }
                continue;
            }
            if (value > data.remaining()) {
                throw new IllegalArgumentException("Truncated CUDA attribute payload");
            }
            int end = data.position() + value;
            if (attribute == 0x0a) { // EIATTR_PARAM_CBANK
                if (value != 8 || base != -1) {
                    throw new IllegalArgumentException("Invalid parameter-bank record");
                }
                data.getInt(); // constant section symbol
                base = Short.toUnsignedInt(data.getShort());
                size = Short.toUnsignedInt(data.getShort());
            }
            else if (attribute == 0x17) { // EIATTR_KPARAM_INFO
                if (value != 12) {
                    throw new IllegalArgumentException("Invalid kernel-parameter record");
                }
                data.getInt();
                int ordinal = Short.toUnsignedInt(data.getShort());
                int offset = Short.toUnsignedInt(data.getShort());
                int length = data.getInt() >>> 18;
                if (parameters.put(ordinal, new Parameter(offset, length)) != null) {
                    throw new IllegalArgumentException("Duplicate parameter ordinal");
                }
            }
            data.position(end);
        }
        if (base == -1) {
            if (parameters.isEmpty()) return null; // device function, not a kernel
            throw new IllegalArgumentException("Missing parameter-bank record");
        }
        if (base + size > 0x10000) {
            throw new IllegalArgumentException("Parameter bank exceeds 64 KiB");
        }
        int ordinal = 0;
        int previousEnd = 0;
        for (var entry : parameters.entrySet()) {
            Parameter parameter = entry.getValue();
            if (entry.getKey() != ordinal++ || parameter.size() <= 0 ||
                parameter.offset() < previousEnd ||
                parameter.offset() + parameter.size() > size) {
                throw new IllegalArgumentException("Invalid kernel parameter layout");
            }
            previousEnd = parameter.offset() + parameter.size();
        }
        return new Layout(base, size, parameters);
    }

    @Override
    public boolean added(Program program, AddressSetView set, TaskMonitor monitor, MessageLog log)
            throws CancelledException {
        for (MemoryBlock code : program.getMemory().getBlocks()) {
            monitor.checkCancelled();
            if (!code.getName().startsWith(".text.")) continue;
            String name = code.getName().substring(6);
            MemoryBlock metadata = program.getMemory().getBlock(".nv.info." + name);
            if (metadata == null ||
                (!set.intersects(code.getStart(), code.getEnd()) &&
                 !set.intersects(metadata.getStart(), metadata.getEnd()))) continue;
            try {
                Layout layout = layout(program, metadata);
                if (layout == null) continue;
                Function function = program.getFunctionManager().getFunctionAt(code.getStart());
                if (function == null) {
                    if (!new CreateFunctionCmd(code.getStart()).applyTo(program, monitor)) {
                        throw new IllegalArgumentException("Could not create kernel function");
                    }
                    function = program.getFunctionManager().getFunctionAt(code.getStart());
                }
                // Explicit signatures belong to the user. An imported signature can be refreshed.
                if (function.getSignatureSource() == SourceType.USER_DEFINED) continue;
                var bank = program.getAddressFactory().getAddressSpace("cbank");
                var parameters = new ArrayList<ParameterImpl>();
                for (var entry : layout.parameters().entrySet()) {
                    Parameter parameter = entry.getValue();
                    var address = bank.getAddress(layout.base() + parameter.offset());
                    parameters.add(new ParameterImpl("arg_" + entry.getKey(),
                        Undefined.getUndefinedDataType(parameter.size()),
                        new VariableStorage(program, address, parameter.size()),
                        program, SourceType.IMPORTED));
                }
                function.updateFunction("__kernel", null, parameters,
                    Function.FunctionUpdateType.CUSTOM_STORAGE, false, SourceType.IMPORTED);
            }
            catch (CancelledException e) {
                throw e;
            }
            catch (Exception e) {
                log.appendMsg(getName(), name + ": " + e.getMessage());
            }
        }
        return true;
    }
}
