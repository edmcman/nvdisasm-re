# How to read the K rules

The following three chapters include the executable K source as written:
integer and floating-point functions, followed by machine transitions.
The code blocks preserve the source syntax, grouping, quoted strings,
conditions, and attributes. Syntax highlighting and visual line wrapping
make the listings fit the page.

`rule L => R requires C` rewrites a matching left side to the right side
when the condition holds. A rewrite inside `<cell> ... </cell>` updates
that cell's matched subterm; other cells in the same rule constrain or
update the same transition. Ellipses retain K's structural matching meaning,
and `~>` sequences computations. K determines matching, function evaluation,
rule priority, and fallback behavior.

The Markdown files remain the executable definition. The publisher also
writes `rules.json`, containing every rule's exact source text, filename,
and line number.

The lifted example later in the document includes actual K reachability claims
and the prover's output. These check the exported IR program's execution;
they supply no hardware equivalence evidence.
