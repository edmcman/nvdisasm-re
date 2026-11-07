package sass;

import ghidra.pcode.emu.BytesPcodeThread;
import ghidra.pcode.emu.PcodeEmulator;
import ghidra.pcode.emu.ThreadPcodeExecutorState;
import ghidra.pcode.exec.PcodeExecutorState;
import ghidra.program.model.address.AddressSpace;
import ghidra.program.model.lang.Language;

/** One thread block: memory spaces are shared by its threads, except per-thread local memory. */
public class BlockEmulator extends PcodeEmulator {
	private PcodeExecutorState<byte[]> global;

	public BlockEmulator(Language language) {
		super(language);
	}

	public BlockEmulator(Language language, PcodeExecutorState<byte[]> global) {
		this(language);
		this.global = global;
	}

	@Override
	protected PcodeExecutorState<byte[]> createSharedState() {
		PcodeExecutorState<byte[]> block = super.createSharedState();
		return global == null ? block : new ThreadPcodeExecutorState<>(global, block) {
			@Override
			protected boolean isThreadLocalSpace(AddressSpace space) {
				return space.getName().equals("shared");
			}
		};
	}

	@Override
	protected BytesPcodeThread createThread(String name) {
		return new BytesPcodeThread(name, this) {
			@Override
			protected ThreadPcodeExecutorState<byte[]> createThreadState(PcodeExecutorState<byte[]> shared,
					PcodeExecutorState<byte[]> local) {
				return new ThreadPcodeExecutorState<>(shared, local) {
					@Override
					protected boolean isThreadLocalSpace(AddressSpace space) {
						return super.isThreadLocalSpace(space) || space.getName().equals("localmem");
					}
				};
			}
		};
	}
}
