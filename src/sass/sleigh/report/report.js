// Renders the whole report from DATA (base64 gzip JSON written by sass.sleigh.coverage):
// {archs, categories: [[name, [opcode]]], summary: {arch: {opcodes, combination_status, opcode_evidence,
// combination_evidence, interaction_evidence}}, classes: [entry], members: {arch: {opcode: [class index]}}}.
const STATUSES = {
  native: 'modeled directly in Ghidra p-code, the operations used by its decompiler and emulator',
  primitive: 'modeled through a named runtime call; our test emulator supplies its behavior, while the decompiler shows the call',
  'partially native': 'native for some of its instructions, opaque for the rest',
  'partially primitive': 'primitive for some of its instructions, opaque for the rest',
  opaque: 'no semantics: Ghidra knows only which registers it reads and writes'};
const ORDER = Object.keys(STATUSES), PARTIAL = {'partially native': 'native', 'partially primitive': 'primitive'};
const EVIDENCE = ['gpu', 'ptxas', 'reference'];   // strongest first
const NAMES = {gpu: 'GPU', ptxas: 'ptxas', reference: 'reference only', none: 'no evidence'};
const LETTERS = {gpu: 'G', ptxas: 'P', reference: 'R'};
const MEANS = {
  native: 'Every instruction with this setting is computed in p-code.',
  primitive: 'Every instruction with this setting is modeled, some through a runtime call.',
  'partially native': 'Some instructions with this setting are computed in p-code; the rest, depending on the other modifiers, have no semantics.',
  'partially primitive': 'Some instructions with this setting are modeled through a runtime call; the rest, depending on the other modifiers, have no semantics.',
  opaque: 'No instruction with this setting has semantics (dataflow only).'};
const HINTS = {gpu: 'On the checked data path of a passing SM89/SM90 GPU check.',
  ptxas: 'Emitted by NVIDIA’s PTX assembler for a probe with known semantics. Compilation evidence, not a hardware run.',
  reference: 'An emulator case with expected results from an independent reference model.',
  none: 'No GPU fixture, PTX probe or reference case recorded.'};
const RAN = {gpu: 'Evidence includes an SM89/SM90 GPU check for at least one combination.', ptxas: 'Evidence includes a PTX probe for at least one combination.', reference: 'Evidence includes an independent reference case for at least one combination.', none: 'No evidence recorded.'};
const EXAMPLE = {arch: 'SM89', op: 'F2F', cls: 'f2f_f64_downconvert__RRR_RRR',
  what: 'an instruction that converts a floating-point number to another floating-point format, for example a 64-bit double to a 32-bit float',
  archName: 'the architecture of NVIDIA’s RTX 40-series (Ada) GPUs, the one we test on real hardware',
  // C: the class's name, read off one of its instructions.
  about: `The instruction <code>F2F.F32.F64 R0, R2</code> belongs to this class. Its class name breaks down as follows:</p><ul class="anatomy">
    <li><code>f2f</code>: the instruction, F2F (“float to float”).</li>
    <li><code>f64_downconvert</code>: the variant that converts <i>down</i> from a 64-bit double to a narrower, less precise format, F32 or F16; the instruction’s <code>.F32.F64</code> reads “to F32 from F64”. A sibling class, <code>f2f_f64_upconvert…</code>, converts up to a double.</li>
    <li><code>RRR_RRR</code>: the operand form. This machine-description suffix identifies the register operand form. In this instruction, both the destination and the source are registers: <code>R0</code> receives the float, and <code>R2</code> holds the double (with <code>R3</code>, since a double takes two 32-bit registers). Sibling classes take the source as an immediate (<code>RIR</code>, a constant inside the instruction), from constant memory (<code>RCR</code>), from a uniform register (<code>RUR</code>, one register shared by a warp’s 32 threads) or from constant memory through a uniform register (<code>RCXR</code>).</li></ul>`,
  // M: what a modifier is, starting from the same instruction.
  modifiers: 'The <code><mark class="format-modifier">.F32.F64</mark></code> in <code>F2F<mark class="format-modifier">.F32.F64</mark> R0, R2</code> is a <b>modifier</b>: a part of the instruction that selects a variant of the operation, here the pair of formats (converting to F32 from F64). Other modifiers select a rounding mode, such as <code>.RZ</code> (round toward zero), or request flush-to-zero behavior for subnormal values (<code>.FTZ</code>). Most can be left out, and then take a default (round to nearest, no flushing); the formats cannot, so every F2F names them.',
  // M: reading the class's box with instructions.
  reading: 'Reading the box: <code>noftz</code> means no flush-to-zero modifier; <code>RN</code> means round to nearest, ties to even; <code>RM</code>, <code>RP</code> and <code>RZ</code> mean round toward negative infinity, positive infinity and zero. <code>Rb</code> is the source operand: <code>Rb@negate</code> and <code>Rb@absolute</code> control negation and absolute value, with <code>unset</code> disabling a flag and <code>set</code> enabling it.</p><p>For example, <code>F2F.F32.F64 R0, R2</code> is the combination that takes <code>F32.F64</code> on the <code>dstfmt.srcfmt</code> line and the first, default chip on every other line. <code>F2F.F32.F64.RZ R0, R2</code> takes <code>RZ</code> instead on the <code>rnd</code> line, and <code>F2F.FTZ.F32.F64 R0, R2</code> takes <code>FTZ</code> on the <code>ftz</code> line.'};
const CHIPS = 'Each line names a modifier; each chip is one choice. Green means all legal combinations with that choice are native; yellow means all are modeled and some need a runtime call; red means none are modeled. A half-colored chip means only some are modeled, depending on other choices; its 50/50 split is a marker, not a percentage. A partial class’s “overall” chip shows its actual combination proportions. Letters mark the strongest recorded evidence for at least one combination with that choice: G = SM89/SM90 GPU check, P = PTX compilation, R = independent reference case. A letter does not cover every combination with that choice. Hover for counts, or expand “combinations with evidence” for the recorded forms; these show modifier settings, not input values.';

const esc = t => String(t).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const pl = (n, one, many) => n === 1 ? one : many;
const slug = s => s.replace(' ', '-');
const sum = xs => xs.reduce((a, b) => a + b, 0);
const best = ks => EVIDENCE.find(k => ks.includes(k)) || 'none';
const badge = ks => best(ks) === 'none' ? '' : `<b class="badge ev-${best(ks)}">${LETTERS[best(ks)]}</b>`;
const partial = counts => sum(Object.keys(PARTIAL).map(p => counts[p] || 0));
const covered = counts => sum(Object.values(counts)) - (counts.opaque || 0);
const total = counts => sum(Object.values(counts));
// Compact, for cells and cards: exact up to 9999, then 12.3k, 606k, 51.4M (tooltips keep the exact counts).
const short = n => n < 1e4 ? `${n}` : n < 1e6 ? `${+(n / 1e3).toFixed(n < 1e5 ? 1 : 0)}k` : `${+(n / 1e6).toFixed(1)}M`;
const count = counts => partial(counts) ? `${short(covered(counts) - partial(counts))}+${short(partial(counts))}/${short(total(counts))}` : `${short(covered(counts))}/${short(total(counts))}`;
const tally = (counts, order, zeros) => order.filter(k => zeros || counts[k]).map(k => `${(counts[k] || 0).toLocaleString('en')} ${NAMES[k] || k}`).join(', ');
// The exact counts behind a compact one, for a tooltip.
const exact = (counts, what) => `${(covered(counts) - partial(counts)).toLocaleString('en')} fully covered${partial(counts) ? `, ${partial(counts).toLocaleString('en')} partly` : ''}, of ${total(counts).toLocaleString('en')} ${what} (${tally(counts, ORDER)})`;
const heat = (n, d) => `--h:${Math.round(120 * n / d)}`;
const gradient = parts => {let at = 0; const t = sum(parts.map(([, n]) => n)) || 1;
  return `linear-gradient(90deg,${parts.map(([c, n]) => `${c} ${100 * at / t}% ${100 * (at += n) / t}%`).join(',')})`};
// Classes by status; a partial class counts half its modeled part, half opaque.
const halves = counts => {const n = {native: counts.native || 0, primitive: counts.primitive || 0, opaque: counts.opaque || 0};
  for (const [p, base] of Object.entries(PARTIAL)) {n[base] += (counts[p] || 0) / 2; n.opaque += (counts[p] || 0) / 2} return n};
const stripColors = counts => gradient(Object.entries(halves(counts)).map(([s, n]) => [`var(--${s})`, n]));
const strip = (counts, inline) => `<i class="${inline ? 'strip inline' : 'strip'}" style="background:${stripColors(counts)}"></i>`;

function main(D) {
  const {archs, categories, summary, classes, members} = D;
  const at = (arch, op) => ['opcodes', 'combination_status', 'opcode_evidence', 'combination_evidence'].map(k => summary[arch][k][op] || {});
  const state = {text: '', ev: '', arch: null, open: null};
  const grid = document.getElementById('grid');

  function cell(arch, op, tag = 'td', interactive = true) {
    const [c, m, ce, me] = at(arch, op);
    if (!total(c)) return tag === 'td' ? '<td class="absent"></td>' : '';
    const verified = (e, n) => n ? `\n   evidence: ${tally(e, [...EVIDENCE, 'none'], true)}` : '';
    const tip = `${op} on ${arch}${interactive ? ': click for details' : ''}\n`
      + `C: ${covered(c)} of ${total(c)} classes have semantics (${tally(c, ORDER)})${verified(ce, covered(c))}\n`
      + `M: ${covered(m).toLocaleString('en')} of ${total(m).toLocaleString('en')} modifier combinations have semantics (${tally(m, ORDER)})${verified(me, covered(m))}`;
    const kinds = [...EVIDENCE, 'none'].filter(k => ce[k] || me[k]);
    const attrs = interactive ? ` role="button" tabindex="0" data-arch="${arch}" data-ev="${kinds.join(' ')}"${covered(c) ? ' data-sem' : ''}` : '';
    return `<${tag} class="cell heat" style="${heat(covered(c) - partial(c) / 2, total(c))}"${attrs} title="${esc(tip)}">`
      + `<span class="line"><i>C</i>${count(c)}</span><span class="line values"><i>M</i>${count(m)}</span>${strip(c)}</${tag}>`;
  }

  function card(arch) {
    const s = summary[arch], ops = Object.values(s.opcodes), withSem = ops.filter(c => covered(c)).length;
    const add = (a, b) => {for (const [k, n] of Object.entries(b)) a[k] = (a[k] || 0) + n; return a};
    const status = ops.reduce(add, {}), combos = Object.values(s.combination_status).reduce(add, {}), ev = Object.values(s.opcode_evidence).reduce(add, {});
    const percent = Math.round(100 * withSem / ops.length);
    return `<div class="card heat" data-arch="${arch}" role="button" tabindex="0" title="Filter to opcodes present on ${arch}; the bottom strip splits its classes by status" style="${heat(percent, 100)}">`
      + `<h2>${arch}</h2><div class="big">${percent}%</div><div class="sub">of opcodes have some semantics</div>`
      + `<dl><dt>opcodes</dt><dd>${withSem}/${ops.length}</dd><dt>classes</dt><dd title="${exact(status, 'classes')}">${count(status)}</dd><dt>combinations</dt><dd title="${exact(combos, 'modifier combinations')}">${count(combos)}</dd></dl>`
      + `<div class="sub h">classes by strongest evidence</div><dl>${EVIDENCE.filter(k => ev[k]).map(k => `<dt>${NAMES[k]}</dt><dd>${ev[k]}</dd>`).join('')}</dl>${strip(status)}</div>`;
  }

  function overall(e) {
    const n = halves(e.combinations), t = total(e.combinations);
    const split = ORDER.filter(s => e.combinations[s]).map(s => `${e.combinations[s]} ${s}`).join(', ');
    return `<span class="chip" style="background:${gradient(Object.entries(n).map(([s, k]) => [`var(--c-${s})`, k]))}" title="overall: of its ${t} modifier ${pl(t, 'combination', 'combinations')}, ${split}. ${RAN[best(e.kinds)]}">overall${badge(e.kinds)}</span>`;
  }
  const head = e => `<h4><code>${esc(e.cls)}</code> ${e.status in PARTIAL ? overall(e)
    : `<span class="chip st-${slug(e.status)}" title="${e.status}. ${RAN[best(e.kinds)]}">${e.status}${badge(e.kinds)}</span>`}</h4>`;
  function body(e) {
    const mods = {}, forms = {};
    for (const [m, l, s, ks, combos, tested] of e.values) if (m !== null) {
      const t = sum(Object.values(tested)) - (tested.none || 0), split = ORDER.filter(st => combos[st]).map(st => `${combos[st].toLocaleString('en')} ${st}`).join(', ');
      const ran = t ? `evidence recorded for ${t.toLocaleString('en')} (${EVIDENCE.filter(k => tested[k]).map(k => `${tested[k]} ${NAMES[k]}`).join(', ')})` : 'no evidence recorded';
      (mods[m] = mods[m] || []).push(`<span class="chip st-${slug(s)}" title="${esc(m)}=${esc(l)}: ${s}. ${MEANS[s]}\n${total(combos).toLocaleString('en')} ${pl(total(combos), 'combination has', 'combinations have')} it: ${split}; ${ran}.">${esc(l)}${badge(ks)}</span>`);
    }
    for (const [k, fs] of Object.entries(e.forms)) for (const f of fs) (forms[f] = forms[f] || []).push(k);
    const tested = Object.keys(forms).sort();
    return (Object.entries(mods).map(([m, cs]) => `<div class="mod"><code>${esc(m)}</code>${e.modifier_roles?.[m] ? `<small>${esc(e.modifier_roles[m])}</small>` : ''}${cs.join('')}</div>`).join('') || '<div class="mod">no modifiers</div>')
      + (tested.length ? `<details><summary>${tested.length} ${pl(tested.length, 'combination', 'combinations')} with evidence</summary><ul>${tested.map(f => `<li><code>${esc(f) || '(defaults)'}</code> ${badge(forms[f])}</li>`).join('')}</ul></details>` : '')
      + interactions(e.interaction);
  }

  function interactions(e) {
    if (!e || !e.total) return '';
    const n = value => value.toLocaleString('en');
    const title = e.strength === 'exhaustive' ? 'Exhaustive interactions' : `${e.strength}-wise interactions`;
    return `<div class="evidence-wrap"><table class="evidence"><caption>${title}</caption>`
      + `<thead><tr><th>Evidence</th><th>Covered</th><th>Missing</th></tr></thead><tbody>`
      + EVIDENCE.map(k => `<tr><th>${k === 'reference' ? 'Reference cases available' : k === 'ptxas' ? 'PTX compilation' : 'Recorded GPU passes'}${badge([k])}</th><td>${n(e.covered[k])} / ${n(e.total)}</td><td>${n(e.total - e.covered[k])}</td></tr>`).join('')
      + '</tbody></table></div>';
  }

  function verification(classEvidence, combinationEvidence) {
    const classes = total(classEvidence), combinations = total(combinationEvidence);
    const number = n => n.toLocaleString('en');
    const meter = (part, n, k) => {
      const value = part[k] || 0, percent = n ? 100 * value / n : 0;
      return `<td><span class="evidence-count">${number(value)} <span>of ${number(n)}</span></span>`
        + `<i class="vbar" style="background:linear-gradient(90deg,var(--${k === 'none' ? 'muted' : 'v-' + k}) ${percent}%,var(--v-none) 0)"></i></td>`;
    };
    const row = k => `<tr><th scope="row"><span class="evidence-name">${badge([k])}${NAMES[k]}</span>`
      + `<span class="evidence-description">${HINTS[k]}</span></th>${meter(classEvidence, classes, k)}${meter(combinationEvidence, combinations, k)}</tr>`;
    return `<div class="evidence-wrap"><table class="evidence"><caption>Evidence for classes and combinations with semantics. Opaque forms are excluded.</caption>`
      + `<thead><tr><th scope="col">Strongest evidence</th><th scope="col">Classes<span>At least one combination</span></th>`
      + `<th scope="col">Combinations<span>Individual forms</span></th></tr></thead>`
      + `<tbody>${[...EVIDENCE, 'none'].map(row).join('')}</tbody></table></div>`;
  }

  function reportGuide(arch, op, ex) {
    const [c, m] = at(arch, op), n = value => value.toLocaleString('en');
    let out = `<h2>Guide to reading the report</h2><p>There’s a lot to take in, so we recommend reading through the F2F example below. It walks through classes, modifiers and evidence step by step. Use this guide as a quick reference; its examples come from ${arch}, and the same visual cues apply throughout the report.</p>`
      + `<h3>Reading a cell</h3><p>Each column is a GPU architecture (an SM version); each row is an opcode, such as <code>F2F</code> (float-to-float conversion). A cell gives two views of coverage: <b>C</b> counts instruction classes (operation variants and operand forms), while <b>M</b> counts the legal modifier combinations within those classes (choices such as formats and rounding modes).</p><p><b>C counts classes.</b> For F2F, <b>${count(c)}</b> means ${n(covered(c) - partial(c))} classes have complete semantics coverage, ${n(partial(c))} have partial coverage, and ${n(total(c) - covered(c))} have none, out of ${n(total(c))} classes.</p><p><b>M counts modifier combinations.</b> For F2F, <b>${count(m)}</b> means ${n(covered(m))} of ${n(total(m))} combinations have semantics. The partly covered classes contribute their covered combinations to this count.</p><p>Large counts use k for thousand and M for million, such as 12k or 1.2M. Hover a cell for exact counts.</p><p>The background gives a quick view of class coverage, and the strip along the bottom shows how those classes are implemented.</p>`
      + `<div class="color-examples">${[
        ['FADD', 'Computed directly in Ghidra', 'Green in the background means complete class coverage. The green strip means native semantics, computed directly in Ghidra p-code.'],
        [op, 'Some forms are covered', 'The background gives each partly covered class half credit. In the strip, each partially native class is half green and half red, regardless of the fraction of its combinations covered.'],
        ['MUFU', 'Covered with runtime helpers', 'The background is green because all classes are covered. The yellow strip means they need runtime helpers.'],
        ['HMMA', 'Not modeled yet', 'The red background and red strip mean all classes are opaque.']
      ].filter(([name]) => total(at(arch, name)[0])).map(([name, label, description]) => {
        const counts = at(arch, name)[0];
        return `<div class="color-example"><div class="color-sample"><code>${esc(name)}</code>${cell(arch, name, 'div', false)}</div>`
          + `<div><b>${label}</b><p>${esc(tally(counts, ORDER))} ${pl(total(counts), 'class', 'classes')}. ${description}</p></div></div>`;
      }).join('')}</div>`
      + `<p>The cell background uses C, not M: a fully covered class counts as 1, a partly covered class as ½ and an opaque class as 0. For ${esc(op)}, that gives a visual score of ${Math.round(100 * (covered(c) - partial(c) / 2) / total(c))}%, while ${Math.round(100 * covered(m) / total(m))}% of its modifier combinations have semantics.</p>`;
    out += `<p>Category rows count opcodes with any semantics, even partial coverage. Categories follow <a href="https://docs.nvidia.com/cuda/cuda-binary-utilities/index.html#instruction-set-reference">NVIDIA’s SASS reference</a>.</p>`;
    const format = ex.values.find(([mod, label]) => mod === 'dstfmt.srcfmt' && label === 'F32.F64');
    if (format) {
      const counts = format[4];
      out += `<h3>Looking inside a class</h3><p>Click a cell to see its classes and modifier choices. The small labels, or chips, show coverage within a class. Here are two from the F2F class <code>${esc(ex.cls)}</code>.</p>`
        + `<div class="color-examples"><div class="color-example"><div class="color-sample"><code>format choice</code>`
        + `<span class="chip st-${slug(format[2])}">${esc(format[1])}${badge(format[3])}</span></div>`
        + `<div><b>Partial modifier coverage</b><p>Of the ${n(total(counts))} combinations with this format, ${n(counts.native || 0)} are native with <code>noftz</code> and ${n(counts.opaque || 0)} are opaque with <code>FTZ</code>. The format choice alone does not determine coverage; the <code>ftz</code> choice matters too. Split modifier chips always use equal halves, even when the counts are unequal.</p></div></div>`
        + `<div class="color-example"><div class="color-sample"><code>whole class</code>${overall(ex)}</div>`
        + `<div><b>Coverage across the class</b><p>Of all ${n(total(ex.combinations))} combinations in this class, ${ORDER.filter(status => ex.combinations[status]).map(status => `${n(ex.combinations[status])} are ${status}`).join(' and ')}. The “overall” chip’s colored widths show those actual proportions.</p></div></div></div>`;
    }
    return out + `<h3>Reading the badges</h3><p><b>Coverage and testing are separate.</b> Covered means we model the operation; evidence describes its supporting checks.</p><p>The letter badges show the strongest recorded evidence: ${badge(['gpu'])} an SM89/SM90 GPU check, ${badge(['ptxas'])} a PTX compilation probe, or ${badge(['reference'])} an independent reference case. A badge applies to at least one combination with that choice; it does not mean every combination was checked. No letter means no evidence is recorded for that choice. Hover a chip for counts, or expand its list of combinations with evidence.</p>`;
  }

  // What op on arch's cell says; `example` works one class through it.
  function explain(op, arch, example) {
    const es = ((members[arch] || {})[op] || []).map(i => classes[i]);
    if (!es.length) return `<h2>${esc(op)} on ${arch}</h2><p>${esc(op)} is not in ${arch}’s machine description.</p>`;
    const [c, m, ce, me] = at(arch, op), ex = example && es.find(e => e.cls === example.cls);
    const split = counts => ORDER.filter(s => counts[s]).map(s => `${counts[s].toLocaleString('en')} ${s}`).join(', ');
    const list = (xs, word = 'or') => xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} ${word} ${xs[xs.length - 1]}`;
    const code = x => `<code>${esc(x)}</code>`, n = x => x.toLocaleString('en');
    const reads = (counts, what) => `the cell’s <b>${what}</b> line summarizes this as <b>${count(counts)}</b>`;
    let out = `<h2>${example ? 'Example: ' : ''}${esc(op)} on ${arch}</h2>`
      + (example ? `<p>${esc(op)} is ${esc(example.what)}. ${arch} is ${esc(example.archName)}. This is ${esc(op)}’s cell in the table below:</p>` : '')
      + `<div class="sample">${esc(op)}${cell(arch, op, 'div', false)}<span class="legend">`
      + `<b>C:</b> ${n(covered(c) - partial(c))} classes with complete semantics coverage, ${n(partial(c))} with partial coverage, ${n(total(c) - covered(c))} with none.<br>`
      + `<b>M:</b> ${n(covered(m) - partial(m))} of ${n(total(m))} modifier combinations with complete semantics coverage${partial(m) ? `; ${n(partial(m))} more with partial coverage` : ''}.</span></div>`;

    out += `<h3>C: classes</h3><p>NVIDIA’s machine description groups instructions into <b>classes</b> by operation variant and operand form.`
      + (ex ? ` For example, ${code(ex.cls)} is one of ${esc(op)}’s ${total(c)} classes. ${example.about}` : '</p>')
      + `<p>A class is <b>covered</b> if our Ghidra module models what at least one of its instructions computes, rather than only which registers it reads and writes, and <b>fully covered</b> if it models all of them. A modeled instruction is <i>native</i> when Ghidra’s p-code computes its result and <i>primitive</i> when a runtime helper is necessary; the others are <i>opaque</i>. A class with some modeled instructions and some opaque ones is <i>partially native</i> or <i>partially primitive</i>. These labels describe our implementation, not whether the GPU supports the instruction.</p><p>On ${arch}, ${esc(op)}’s ${total(c)} ${pl(total(c), 'class is', 'classes are')} ${split(c)}; ${reads(c, 'C')}.</p>`
      + `<p>To see which instructions in a class are native, need a runtime helper or remain opaque, we look at its modifier combinations.</p>`;

    out += `<h3>M: modifier combinations</h3>`
      + (ex ? `<p>${example.modifiers}</p>` : `<p>Within a class, <b>modifiers</b> select variants of the operation, such as a rounding mode (${code('.RZ')}) or flush-to-zero behavior for subnormal values (${code('.FTZ')}); most can be left out and then take a default.</p>`)
      + `<p>A <b>combination</b> picks one choice for every modifier of a class, so it identifies an instruction form within that class. Register numbers, immediate values, input data and scheduling hints do not multiply this count. The same modifier settings in two classes count separately. On ${arch}, ${esc(op)}’s classes have ${n(total(m))} ${pl(total(m), 'combination', 'combinations')}: ${split(m)}; ${reads(m, 'M')}.</p>`;
    if (ex) {
      const mods = {};
      for (const [mod, l, st] of ex.values) if (mod !== null) (mods[mod] = mods[mod] || []).push([l, st]);
      const sizes = Object.values(mods).map(v => v.length), legal = total(ex.combinations);
      const ruled = Object.entries(ex.illegal || {}).map(([mod, ls]) => `${code(mod)} ${list(ls.map(code))}`);
      out += `<p>The class ${code(ex.cls)} has ${sizes.length} modifiers: ${list(Object.entries(mods).map(([mod, v]) => `${code(mod)} (${v.length}${pl(v.length, ' choice', ' choices')})`), 'and')}`
        + (ruled.length ? ` (the machine description rules out ${list(ruled, 'and')} here)` : '')
        + `, so ${sizes.reduce((a, b) => a * b, 1) === legal ? `${sizes.join(' × ')} = ` : ''}${n(legal)} combinations. Here they are, one line per modifier and one chip per choice:</p>`
        + `<div class="classes">${head(ex) + body(ex)}</div>`
        + `<p>${example.reading}</p>`
        + `<p>Of this class’s ${n(legal)} combinations, ${ORDER.filter(s => ex.combinations[s]).map((s, i) => `${n(ex.combinations[s])} ${i ? '' : pl(ex.combinations[s], 'is ', 'are ')}${s}`).join(' and ')}, so this one class contributes 1 to the partly covered term of C, and ${n(covered(ex.combinations))} covered combinations to M.</p>`
        + `<p>In this example, <code>noftz</code> has a ${badge(['gpu'])}, but its tooltip shows how many of its combinations have GPU evidence. Expand “combinations with evidence” to see the recorded forms. Omitted operand flags are unset.</p>`;
    }
    if (covered(c)) out += `<h3>Testing evidence</h3>`
      + verification(ce, me)
      + `<p class="evidence-note">Each class or combination is counted once, under its strongest recorded evidence: GPU, then ptxas, then reference. GPU evidence applies only to SM89 and SM90. Reference evidence records available test cases; this report does not run them or show their latest results.</p>`
      + (ex ? `<p>For ${esc(op)} on ${arch}, GPU evidence covers at least one combination in ${n(ce.gpu || 0)} of the ${n(covered(c))} modeled classes, but just ${n(me.gpu || 0)} of the ${n(covered(m))} modeled combinations. ${n(me.none || 0)} modeled combinations have no recorded evidence. This is why class coverage, combination coverage and evidence are shown separately.</p>` : '');

    if (covered(c)) out += `<h3>Interaction evidence</h3>`
      + interactions(summary[arch].interaction_evidence?.[op])
      + `<p class="evidence-note">Interaction requirements include every legal tuple up to the configured strength, exhaustively for smaller classes. Hints and consistency controls participate; scheduling and reuse fields are excluded. Each tuple must extend to a complete legal form with modeled semantics. Partial-form adapter gaps remain visible.</p>`
      + `<p class="evidence-note">These sources are counted independently. Only positive reference forms contribute; raw reference provenance above also includes diagnostic error/fallback checks. GPU passes apply to this architecture and do not verify the CUDA concurrency model or emulator-only effect assertions. PTX compilation provides encoding evidence.</p>`;

    return out + (ex ? '' : `<h3>${esc(op)}’s classes</h3><p>${CHIPS}</p><div class="classes">${es.map(e => head(e) + body(e)).join('')}</div>`);
  }

  function categoryCell(cells) {
    const present = cells.filter(c => !c.classList.contains('absent')), n = present.filter(c => 'sem' in c.dataset).length;
    return present.length ? [`${n}/${present.length}`, heat(n, present.length)] : null;
  }

  // ---- static parts
  document.getElementById('ev').innerHTML = `<option value="">all evidence levels</option>` + [...EVIDENCE, 'none'].map(k => `<option value="${k}">${k === 'none' ? 'modeled, no evidence' : NAMES[k]}</option>`).join('');
  const cards = document.getElementById('cards');
  cards.innerHTML = archs.map(card).join('');
  grid.innerHTML = `<thead><tr><th title="C: classes with semantics / classes; M: modifier combinations with semantics / modifier combinations">opcode <small>(C, M)</small></th>`
    + archs.map(a => `<th data-arch="${a}" role="button" tabindex="0" title="Filter to opcodes present on ${a}; keep all columns for comparison">${a}</th>`).join('') + '</tr></thead>'
    + categories.map(([name, ops]) => `<tbody><tr class="category"><th>${esc(name)}</th>${archs.map(() => '<td></td>').join('')}</tr>`
      + ops.map(op => `<tr class="op"><th>${esc(op)}</th>${archs.map(a => cell(a, op)).join('')}</tr>`).join('') + '</tbody>').join('');
  const exampleMembers = (members[EXAMPLE.arch] || {})[EXAMPLE.op];
  if (exampleMembers) {
    const exampleClass = exampleMembers.map(i => classes[i]).find(e => e.cls === EXAMPLE.cls);
    if (exampleClass) document.getElementById('guide').innerHTML = reportGuide(EXAMPLE.arch, EXAMPLE.op, exampleClass);
    document.getElementById('example').innerHTML = explain(EXAMPLE.op, EXAMPLE.arch, EXAMPLE);
  }

  // ---- state
  function apply() {
    const col = archs.indexOf(state.arch) + 1, has = c => !state.ev || (c.dataset.ev || '').split(' ').includes(state.ev);
    for (const r of grid.querySelectorAll('tr.op')) {
      const cs = [...r.children].slice(1);
      r.hidden = !r.firstChild.textContent.includes(state.text) || (col > 0 && cs[col - 1].classList.contains('absent')) || !(col > 0 ? has(cs[col - 1]) : cs.some(has));
      for (const c of cs) c.classList.toggle('dim', !!state.ev && c.classList.contains('cell') && !has(c));
    }
    for (const b of grid.tBodies) {
      const rows = [...b.rows].filter(r => r.classList.contains('op') && !r.hidden);
      b.hidden = !rows.length;
      [...b.rows[0].cells].slice(1).forEach((c, i) => {
        const v = categoryCell(rows.map(r => r.cells[i + 1]));
        c.className = v ? 'heat' : 'absent'; c.textContent = v ? v[0] : ''; c.style.cssText = v ? v[1] : '';
        c.title = 'visible opcodes with any semantics / visible opcodes present on this architecture in this category';
      });
    }
    for (const d of grid.querySelectorAll('tr.detail')) d.hidden = d.previousElementSibling.hidden;
    document.getElementById('empty').hidden = !!grid.querySelector('tr.op:not([hidden])');
    const only = document.getElementById('only');
    only.hidden = !state.arch;
    only.innerHTML = state.arch ? `opcodes present on ${state.arch} <button aria-label="Clear architecture filter">×</button>` : '';
    for (const e of [...grid.tHead.rows[0].cells, ...cards.children]) e.classList.toggle('active', !!state.arch && e.dataset.arch === state.arch);
  }
  function close() {
    for (const d of grid.querySelectorAll('tr.detail')) d.remove();
    for (const c of grid.querySelectorAll('td.chosen')) c.classList.remove('chosen');
  }
  function show(td) {
    const r = td.parentElement, arch = td.dataset.arch, op = r.firstChild.textContent;
    const again = state.open && state.open.op === op && state.open.arch === arch;
    close(); state.open = again ? null : {op, arch};
    if (again) return;
    td.classList.add('chosen');
    const d = document.createElement('tr');
    d.className = 'detail'; d.innerHTML = `<td colspan="${archs.length + 1}"><section class="explain">${explain(op, arch)}</section></td>`;
    r.after(d);
  }
  const pick = arch => {state.arch = state.arch === arch ? null : arch; apply()};
  const activate = (e, f) => {if (e.type === 'click' || e.key === 'Enter' || e.key === ' ') {e.preventDefault(); f()}};
  for (const type of ['click', 'keydown']) {
    grid.addEventListener(type, e => {const td = e.target.closest('td.cell'); if (td && grid.contains(td)) activate(e, () => show(td))});
    grid.tHead.addEventListener(type, e => {const th = e.target.closest('th[data-arch]'); if (th) activate(e, () => pick(th.dataset.arch))});
    cards.addEventListener(type, e => {const c = e.target.closest('.card'); if (c) activate(e, () => pick(c.dataset.arch))});
  }
  document.getElementById('only').addEventListener('click', e => {if (e.target.closest('button')) pick(state.arch)});
  document.addEventListener('keydown', e => {if (e.key === 'Escape') {close(); state.open = null}});
  document.getElementById('q').addEventListener('input', e => {state.text = e.target.value.trim().toUpperCase(); apply()});
  document.getElementById('ev').addEventListener('change', e => {state.ev = e.target.value; apply()});
  apply();
}

new Response(new Blob([Uint8Array.from(atob(DATA), c => c.charCodeAt(0))]).stream().pipeThrough(new DecompressionStream('gzip'))).json().then(main);
