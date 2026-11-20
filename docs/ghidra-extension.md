# SASS Ghidra extension

The SASS extension adds NVIDIA SASS languages, a CUDA cubin loader, and kernel-argument analysis to Ghidra. The release ZIP contains all generated architectures and both floating-point variants; it does not require Python or CUDA to install.

## Install

1. Download `sass-extension.zip` from the [latest release](https://github.com/edmcman/nvdisasm-re/releases/latest).
2. If you previously installed the processor manually, remove its `SASS` directory from `Ghidra/Processors` before proceeding. Installing both copies causes a duplicate module error.
3. In Ghidra's project window, choose **File → Install Extensions**. Click the plus button, select the ZIP, and restart Ghidra when prompted.

The full test suite targets Ghidra 12.2 with Java 25. The release also receives SM89 cubin import, disassembly, and kernel-argument analysis smoke checks on Ghidra 12.0.4 and 12.1.4 with Java 21. These checks do not establish comprehensive compatibility. The JAR is built against 12.0.4 and the extension metadata names that version; Ghidra may warn about the version when installing it on newer releases.

## Update or remove

In **File → Install Extensions**, uncheck SASS and restart Ghidra to remove the installed version. To update, install the new release ZIP using the steps above and restart again. If you installed the processor manually, update or remove that copy separately; the extension manager does not manage it.

## Import a cubin

Create or open a Ghidra project and import a `.cubin` file. To extract cubins from a CUDA fat binary, use `cuobjdump -xelf`. Select the **CUDA Cubin** loader if prompted. It selects the matching `SASS:LE:64:smXX` language from the cubin's ELF header. Cubin sections all have address 0, so the loader gives each kernel's code and constant data its own `ram` address. Kernel addresses are therefore offset from the per-kernel addresses that nvdisasm prints.

The default language uses hardware-exact floating-point semantics. Each architecture also has a `smXX_readable` language, selectable in the import dialog, that uses plain floating-point formulas and omits details such as rounding modes and denormal flushing. See [semantics.md](semantics.md) for coverage and limitations.

Run automatic analysis after import. **SASS Kernel Arguments** reads parameter offsets and sizes from kernel metadata and adds parameters to function signatures when available. Names and types absent from the metadata remain unknown, and user-edited signatures are preserved. Existing programs can be analyzed again after installing the extension to populate their arguments.

For building from source and additional details about the loader and analysis, see the [Ghidra processor section of the README](../README.md#ghidra-processor-processorsass).
