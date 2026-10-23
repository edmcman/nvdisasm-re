"""The SASS language's register files, shared by the generator and the Ghidra-side programs (which
import only this, so editing the generator leaves their result caches valid)."""
REG_FILES = {"R": (0x0000, 4, 256, 255, "RZ"), "UR": (0x0400, 4, 64, 255, "URZ"),
             "P": (0x0800, 1, 8, 7, "PT"), "UP": (0x0808, 1, 8, 7, "UPT")}
ENUM_FILE = {"Register": "R", "NonZeroRegister": "R", "ZeroRegister": "R",
             "UniformRegister": "UR", "NonZeroUniformRegister": "UR", "ZeroUniformRegister": "UR",
             "Predicate": "P", "Predicate_vimnmx": "P", "UniformPredicate": "UP"}
