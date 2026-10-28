package sass;

import java.io.IOException;
import java.util.Collection;
import java.util.List;

import ghidra.app.util.bin.ByteProvider;
import ghidra.app.util.bin.format.elf.ElfConstants;
import ghidra.app.util.bin.format.elf.ElfException;
import ghidra.app.util.bin.format.elf.ElfHeader;
import ghidra.app.util.opinion.ElfLoader;
import ghidra.app.util.opinion.LoadSpec;
import ghidra.app.util.opinion.LoaderTier;
import ghidra.program.model.data.ArrayDataType;
import ghidra.program.model.data.ByteDataType;
import ghidra.program.model.listing.Program;
import ghidra.program.model.util.CodeUnitInsertionException;

/** ELF cubin import with a global byte region for offsets into kernel arguments. */
public class SassCubinLoader extends ElfLoader {
    @Override
    public String getName() {
        return "CUDA Cubin";
    }

    @Override
    public LoaderTier getTier() {
        return LoaderTier.SPECIALIZED_TARGET_LOADER;
    }

    @Override
    public Collection<String> getAssociatedFileExtensions() {
        return List.of("cubin");
    }

    @Override
    public Collection<LoadSpec> findSupportedLoadSpecs(ByteProvider provider) throws IOException {
        try {
            if (new ElfHeader(provider, null).e_machine() != ElfConstants.EM_CUDA) {
                return List.of();
            }
        }
        catch (ElfException e) {
            return List.of();
        }
        // Keep the ELF opinion mappings and options; only the final data setup differs.
        return new ElfLoader().findSupportedLoadSpecs(provider).stream()
            .map(spec -> new LoadSpec(this, spec.getDesiredImageBase(),
                spec.getLanguageCompilerSpec(), spec.isPreferred()))
            .toList();
    }

    @Override
    protected void createDefaultMemoryBlocks(Program program, ImporterSettings settings) {
        super.createDefaultMemoryBlocks(program, settings);
        if (!"SASS".equals(program.getLanguage().getProcessor().toString())) {
            return;
        }
        int transaction = program.startTransaction("Define kernel argument byte region");
        try {
            for (var symbol : program.getLanguage().getDefaultSymbols()) {
                if (!"kernel_arguments".equals(symbol.getLabel())) {
                    continue;
                }
                // Reference analysis may split undefined arrays. A byte array
                // retains the global extent without choosing per-kernel types.
                program.getListing().createData(symbol.getAddress(),
                    new ArrayDataType(ByteDataType.dataType, symbol.getByteSize(), 1));
            }
        }
        catch (CodeUnitInsertionException e) {
            settings.log().appendMsg("Could not define kernel argument byte region: " + e.getMessage());
        }
        finally {
            program.endTransaction(transaction, true);
        }
    }
}
