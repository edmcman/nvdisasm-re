# How to read the operational rules

The rules in the following three chapters are generated directly from the
executable K source, including the integer and floating-point functions and
the machine transitions. Each rule cites its source file and line. The publisher
changes layout and the rewrite arrow; it does not invent judgments, combine
steps, rename operators, or replace K terms with mathematical approximations.

A naturally sized inference rule puts the literal `requires` condition above
the bar and the rewrite below it. Unconditional rules have an empty premise.
The source location is printed beneath the rule. The arrow
$\Longrightarrow$ is the source token `=>`. Attributes such as `[priority(40)]`
and `[owise]` are retained below the rewrite. Cells occupy separate lines.
K determines matching, evaluation,
rule priority, and fallback behavior; the page layout introduces no new
execution relation. Rules with `[function]` symbols compute values; rules
over configuration cells describe machine execution.

In a cell, `<regs> R` $\Longrightarrow$ `overlay(R, WR) </regs>` updates
the matched register map. Other cells in the same rule constrain or update
the same transition. Ellipses retain K's structural matching meaning, and
`~>` retains K's computation sequencing meaning. A local rewrite changes
only its matched subterm, so an ellipsis is surrounding context rather than
a value introduced by the right-hand side.

The source Markdown files remain the executable definition. The publisher
also writes `rules.json`, containing the exact source text and locations
of every rendered rule, so the correspondence can be inspected independently.
This is a source presentation, not an independently axiomatized semantics
or a proof that the lifted program matches NVIDIA hardware.
