# SASS extension for Ghidra

This extension adds NVIDIA SASS (the native GPU instruction set) to Ghidra. It provides disassembly and semantics for CUDA GPU code, a loader for CUDA cubin files, and an analyzer that recovers kernel arguments. It includes the SM75, SM80, SM86, SM89, SM90, SM100, SM101, SM103, SM107, and SM120 architectures, each in two floating-point variants. Installing it does not require Python or CUDA.

## Supported Ghidra versions

The extension supports Ghidra 12.0.4 through 12.2. It was tested on Ghidra 12.0.4 and 12.1.4 with Java 21, and on 12.2 with Java 25.

The extension is built for Ghidra 12.0.4. If you install it on any other version, such as 12.1.4 or 12.2, Ghidra warns that the extension version does not match. This warning is expected, and you can safely continue with the installation.

## Install

1. In Ghidra's project window, choose **File → Install Extensions**.
2. Click the plus button, select `sass-extension.zip`, and restart Ghidra when prompted.

## Update or remove

To remove the extension, open **File → Install Extensions**, uncheck SASS, and restart Ghidra. To update, remove the installed version, then install the newer `sass-extension.zip` following the steps above and restart again. The extension manager does not manage a manually installed processor, so update or remove that copy separately.

## Import a cubin

Create or open a Ghidra project and import a `.cubin` file. To extract cubins from a CUDA fat binary, use `cuobjdump -xelf`. Select the **CUDA Cubin** loader if prompted. The loader picks the matching `SASS:LE:64:smXX` language from the cubin's ELF header. All cubin sections have address 0, so the loader gives each kernel's code and constant data its own address. Kernel addresses in Ghidra are therefore offset from the per-kernel addresses that nvdisasm prints.

Each architecture has two languages:

- `smXX` (the default) uses hardware-exact floating-point semantics.
- `smXX_readable` uses plain floating-point formulas, leaving out details such as rounding modes and denormal flushing. You can select it in the import dialog.

Run automatic analysis after import. The **SASS Kernel Arguments** analyzer reads parameter offsets and sizes from the kernel metadata and adds them to function signatures when available. The metadata does not include names or types, so these stay unknown. Signatures you edited yourself are kept. To fill in arguments for programs imported before the extension was installed, analyze them again.

## Try the bundled example

Extract `sass-extension.zip` and import `SASS/examples/write_value.cubin` into a Ghidra project using the **CUDA Cubin** loader. It selects `SASS:LE:64:sm89`. Run automatic analysis, then open the `write_value` function. Its first instruction is `MOV`, and its two arguments are an 8-byte device pointer followed by a 4-byte scalar value. The kernel writes the value to the supplied pointer; the analyzer recovers the argument sizes, leaving names and types unknown.

The corresponding SASS source is in `SASS/examples/write_value.sass`. You can inspect and analyze the example without Python, CUDA, or a GPU.

## Coverage

**Disassembly.** Instruction decoding comes from the instruction tables built into NVIDIA's `nvdisasm` (CUDA 13.4). Disassembly is expected to be complete for every supported architecture.

**Semantics.** Semantics describe what each instruction does. They are the basis for Ghidra's decompiler and emulator. This work is still in progress and is not complete, but it covers many of the most common instructions. Instructions without semantics still disassemble and keep their control flow, and the decompiler shows them as calls such as `sass_opaque_HMMA(...)`.

**Best-supported architectures.** We only have access to SM89 and SM90 GPUs, so those are the only architectures whose semantics have been checked against real hardware. SM89 and SM90 have the best support right now.

## Known issues

Ghidra 12.2's **GCC Exception Handlers** analyzer can report `Error analyzing GCC DebugFrame exception tables` followed by `ExtLength is not completely implemented.` when a cubin contains extended-length DWARF `.debug_frame` records. This aborts frame-metadata analysis without itself indicating a SASS decoding failure. See [Ghidra issue #10291](https://github.com/NationalSecurityAgency/ghidra/issues/10291), which requests graceful handling of this unsupported format.

## Contact

If you run into problems or have questions, contact Edward J. Schwartz directly at eschwartz@cert.org.
