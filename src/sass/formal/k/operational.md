# How to read the operational rules

The rules in the following three chapters are generated directly from the
executable K source, including the integer and floating-point functions and
the machine transitions. Each rule cites its source file and line. The publisher
parses the term syntax and applies the mathematical notation below. It does not
invent judgments, combine steps, or replace K terms with approximations.

A naturally sized inference rule puts the `requires` condition above
the bar and the rewrite below it. Unconditional rules have an empty premise.
The source location is printed beneath the rule. The arrow
$\Longrightarrow$ is the source token `=>`. Attributes such as `[priority(40)]`
and `[owise]` are retained below the rewrite. Cells occupy separate lines.
Wrapped replacements align in the right-hand column after the rewrite arrow.
K determines matching, evaluation,
rule priority, and fallback behavior; the page layout introduces no new
execution relation. Rules with `[function]` symbols compute values; rules
over configuration cells describe machine execution.

For example, $\langle R\Longrightarrow
\operatorname{overlay}(R,\mathit{WR})\rangle_{\mathrm{regs}}$ updates the
matched register map. Other cells in the same rule constrain or update the
same transition. Ellipses retain K's structural matching meaning. A local
rewrite changes only its matched subterm, so an ellipsis is surrounding
context rather than a value introduced by the right-hand side.

## Mathematical presentation

Variables retain their source names in italics; constructor and function names
use upright letters. Quoted strings retain their exact contents and quotes.
Sort annotations and checked casts remain visible. Syntax declarations and
the exported example remain literal K for comparison.

| K source | Mathematical notation | Meaning |
| :--- | :--- | :--- |
| `+Int`, `-Int`, `*Int`, `^Int` | $+$, $-$, $\cdot$, $a^b$ | Unbounded integer arithmetic |
| `/Int` | $\div_t$ | Integer quotient truncated toward zero |
| `modInt` | $\mathrm{mod}_E$ | Euclidean remainder, always nonnegative |
| `==Int`, `=/=Int`, `<=Int`, `>=Int` | $=$, $\ne$, $\le$, $\ge$ | Integer comparisons |
| `andBool`, `orBool`, `notBool` | $\land$, $\lor$, $\neg$ | K Boolean operations |
| `==Bool`, `==String`, `==K` | $=_B$, $=_S$, $=_K$ | Boolean, string, and term equality |
| `xorBool` | $\oplus_B$ | Boolean exclusive-or |
| `&Int`, `\|Int`, `xorInt` | $\&$, $\mid$, $\oplus$ | Bitwise integer operations |
| `<<Int`, `>>Int` | $\ll$, $\gg$ | K integer shifts |
| `+String` | $+\!\!+_S$ | String concatenation |
| `boolBit(P)` | $\mathbf{1}[P]$ | One if $P$ is true, otherwise zero |
| `absInt(V)`, `minInt(A,B)`, `maxInt(A,B)` | $\lvert V\rvert$, $\min(A,B)$, $\max(A,B)$ | Integer absolute value, minimum, maximum |
| `ListItem(V)` | $[V]$ | Singleton K list |
| `.List`, `.Map`, `.K` | $\epsilon_{\mathrm{List}}$, $\varnothing_{\mathrm{Map}}$, $\bullet_{\mathrm{K}}$ | Empty list, map, computation |
| Collection juxtaposition | $\Vert$ | K concatenation in the source collection's sort |
| `K \|-> V` | $K\mapsto V$ | Map binding |
| `M[K <- V]` | $M[K\leftarrow V]$ | Map update |
| `K in_keys(M)` | $K\in\operatorname{dom}(M)$ | Map key membership |
| `A ~> B` | $A\curvearrowright B$ | Computation sequencing |
| `<name> T </name>` | $\langle T\rangle_{\mathrm{name}}$ | Configuration cell |
| `{T}:>Sort` | $\{T\}\triangleright\mathrm{Sort}$ | K checked cast; not a numeric conversion |

The presentation retains source parentheses and adds parentheses where ordinary
mathematical precedence would obscure K's grouping. In particular, K's
`andBool` and `orBool` share a precedence level, and `^Int` associates to the
left. No Boolean simplification or arithmetic reassociation is performed.
Line breaks occur at parsed operators and argument boundaries, never inside
a quoted string, exponent, or sort annotation. Replacements continue in the
column to the right of $\Longrightarrow$.

The parser covers the syntax used by these three modules and rejects unknown
syntax. It is a publication parser, not a general K frontend. Adding source
syntax requires extending the notation and its checks.

The source Markdown files remain the executable definition. The publisher
also writes `rules.json`, containing the exact source text and locations
of every rendered rule, so the correspondence can be inspected independently.
This is a source presentation, not an independently axiomatized semantics
or a proof that the lifted program matches NVIDIA hardware.
