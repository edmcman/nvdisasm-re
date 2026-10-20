package sass;

import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Function;

import ghidra.app.plugin.processors.sleigh.SleighInstructionPrototype;

/**
 * SleighLanguage's instruction-prototype cache without the caching. Ghidra keys it by a 32-bit hash of
 * the constructor tree and trusts that hash entirely, so a colliding word silently reuses another
 * class's prototype; building every prototype keeps each decode exact.
 */
public class ExactPrototypes extends ConcurrentHashMap<Integer, SleighInstructionPrototype> {
	@Override
	public SleighInstructionPrototype computeIfAbsent(Integer hash,
			Function<? super Integer, ? extends SleighInstructionPrototype> build) {
		return build.apply(hash);
	}
}
