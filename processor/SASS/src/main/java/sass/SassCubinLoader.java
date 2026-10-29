package sass;

import java.io.IOException;
import java.util.Collection;
import java.util.List;

import ghidra.app.util.Option;
import ghidra.app.util.bin.ByteProvider;
import ghidra.app.util.bin.format.elf.ElfConstants;
import ghidra.app.util.bin.format.elf.ElfException;
import ghidra.app.util.bin.format.elf.ElfHeader;
import ghidra.app.util.opinion.ElfLoader;
import ghidra.app.util.opinion.ElfLoaderOptionsFactory;
import ghidra.app.util.opinion.LoadSpec;
import ghidra.app.util.opinion.LoaderTier;
import ghidra.framework.model.DomainObject;
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
        // Keep ELF opinion mappings; customize defaults and final data setup below.
        return new ElfLoader().findSupportedLoadSpecs(provider).stream()
            .map(spec -> new LoadSpec(this, spec.getDesiredImageBase(),
                spec.getLanguageCompilerSpec(), spec.isPreferred()))
            .toList();
    }

    @Override
    public List<Option> getDefaultOptions(ByteProvider provider, LoadSpec spec,
            DomainObject domainObject, boolean loadIntoProgram, boolean mirror) {
        List<Option> options = super.getDefaultOptions(provider, spec, domainObject,
            loadIntoProgram, mirror);
        try {
            ElfHeader elf = new ElfHeader(provider, null);
            if (elf.e_machine() == ElfConstants.EM_CUDA &&
                    Byte.toUnsignedInt(elf.e_ident_abiversion()) < 8 && elf.findImageBase() == 0) {
                // Legacy cubins have overlapping zero-based segments and sections. Ghidra's
                // overlap resolver subtracts file-offset differences from their load address.
                // Leave room below the chosen base for any difference within this file.
                long size = provider.length();
                if (size > Long.MAX_VALUE - 0xfff) return options;
                long base = Math.max(ElfLoaderOptionsFactory.IMAGE64_BASE_DEFAULT,
                    (size + 0xfff) & ~0xfffL);
                for (Option option : options) {
                    if (ElfLoaderOptionsFactory.IMAGE_BASE_OPTION_NAME.equals(option.getName())) {
                        option.setValue(Long.toHexString(base));
                    }
                }
            }
        }
        catch (ElfException e) {
            // The underlying ELF loader reports unreadable headers during import.
        }
        return options;
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
