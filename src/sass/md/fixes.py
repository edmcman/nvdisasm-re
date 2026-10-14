"""Known errors in NVIDIA's machine descriptions, corrected when classes are loaded
(`parse.load_classes`); `out/` keeps the md as shipped. Each entry names the architectures and
classes, the PREDICATES field and its corrected value, and the evidence."""

PREDICATES = [
    (("SM75", "SM80"),
     ("imad_hi__RRC_RRC", "imad_hi_pseudo__RRC_RRC", "imad_hi_x__RRC_RRC", "imad_hi_x_pseudo__RRC_RRC",
      "imad_wide__RRC_RRC", "imad_wide_pseudo__RRC_RRC", "imad_wide_x__RRC_RRC", "imad_wide_x_pseudo__RRC_RRC"),
     "ISRC_C_SIZE", 64,
     "IMAD.HI/.WIDE's c[][] addend is 64 bits: the md says 32 here, but the same instructions' cx[][] "
     "forms say 64, SM86+ say 64 for exactly these classes (plain IMAD stays 32), and ptxas adds a "
     "64-bit pointer parameter with `IMAD.WIDE.U32 R2, R2, R3, c[0x0][0x160]` then `STG [R2]` "
     "(sm_75 and sm_80, 2026-10-14)."),
]


def fix(arch, classes):
    """classes (parse class dicts) with the corrections for `arch` applied."""
    for archs, names, field, value, _ in PREDICATES:
        if arch in archs:
            for c in classes:
                if c["cls"] in names: c["preds"][field] = value
    return classes
