package sass;

import ghidra.app.util.bin.format.elf.ElfConstants;
import ghidra.app.util.bin.format.elf.ElfHeader;
import ghidra.app.util.bin.format.elf.ElfLoadHelper;
import ghidra.app.util.bin.format.elf.ElfProgramHeader;
import ghidra.app.util.bin.format.elf.ElfProgramHeaderConstants;
import ghidra.app.util.bin.format.elf.ElfSectionHeader;
import ghidra.app.util.bin.format.elf.ElfSymbol;
import ghidra.app.util.bin.format.elf.extend.ElfExtension;
import ghidra.program.model.address.Address;
import ghidra.util.NumericUtilities;

/**
 * Cubin memory layout. Every cubin section and PT_LOAD segment has address 0, so Ghidra's
 * default layout overlaps them all, and its overlap reconciliation throws an address overflow
 * when a segment's padding lies within the largest section. Like a relocatable object, sections
 * are packed at distinct addresses instead, and the segments, which only repeat them, are skipped.
 */
public class SassElfExtension extends ElfExtension {
    @Override
    public boolean canHandle(ElfHeader elf) {
        return elf.e_machine() == ElfConstants.EM_CUDA;
    }

    @Override
    public boolean canHandle(ElfLoadHelper elfLoadHelper) {
        return canHandle(elfLoadHelper.getElfHeader()) &&
            "SASS".equals(elfLoadHelper.getProgram().getLanguage().getProcessor().toString());
    }

    @Override
    public String getDataTypeSuffix() {
        return "_CUDA";
    }

    @Override
    public long getAdjustedLoadSize(ElfProgramHeader elfProgramHeader) {
        return elfProgramHeader.getType() == ElfProgramHeaderConstants.PT_LOAD ? 0
            : super.getAdjustedLoadSize(elfProgramHeader);
    }

    @Override
    public long getAdjustedMemorySize(ElfProgramHeader elfProgramHeader) {
        return elfProgramHeader.getType() == ElfProgramHeaderConstants.PT_LOAD ? 0
            : super.getAdjustedMemorySize(elfProgramHeader);
    }

    @Override
    public Address getPreferredSectionAddress(ElfLoadHelper elfLoadHelper,
            ElfSectionHeader elfSectionHeader) {
        var space = getPreferredSectionAddressSpace(elfLoadHelper, elfSectionHeader);
        long offset = 0;
        for (ElfSectionHeader section : elfLoadHelper.getElfHeader().getSections()) {
            if (!section.isAlloc() ||
                getPreferredSectionAddressSpace(elfLoadHelper, section) != space) continue;
            offset = NumericUtilities.getUnsignedAlignedValue(offset, section.getAddressAlignment());
            if (section == elfSectionHeader) break;
            offset += getAdjustedSize(section) / space.getAddressableUnitSize();
        }
        if (space == elfLoadHelper.getProgram().getAddressFactory().getDefaultAddressSpace()) {
            offset += elfLoadHelper.getImageBaseWordAdjustmentOffset();
        }
        return space.getTruncatedAddress(offset, true);
    }

    @Override
    public Long getSectionSymbolRelativeOffset(ElfSectionHeader section, Address sectionBase,
            ElfSymbol elfSymbol) {
        return elfSymbol.getValue() - section.getAddress();
    }
}
