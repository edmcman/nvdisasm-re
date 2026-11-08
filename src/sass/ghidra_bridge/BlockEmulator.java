package sass;

import ghidra.pcode.emu.BytesPcodeThread;
import ghidra.pcode.emu.PcodeEmulator;
import ghidra.pcode.emu.PcodeThread;
import ghidra.pcode.emu.ThreadPcodeExecutorState;
import ghidra.pcode.exec.PcodeExecutorState;
import ghidra.pcode.exec.SuspendedPcodeExecutionException;
import ghidra.program.model.address.AddressSpace;
import ghidra.program.model.lang.Language;

/** One thread block: memory spaces are shared by its threads, except per-thread local memory. */
public class BlockEmulator extends PcodeEmulator {
	private PcodeExecutorState<byte[]> global;

	public static boolean advance(PcodeThread<byte[]> thread) {
		try {
			if (thread.getFrame() == null) {
				thread.stepPcodeOp();
			}
			thread.finishInstruction();
			return true;
		}
		catch (SuspendedPcodeExecutionException error) {
			// JPype's synthetic exception frames retain Python callers and their executors.
			return false;
		}
	}

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
