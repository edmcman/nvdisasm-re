// Renders the whole report from DATA (base64 gzip JSON written by sass.sleigh.coverage):
// {archs, categories: [[name, [opcode]]], summary: {arch: {opcodes, combination_status, opcode_evidence,
// combination_evidence}}, classes: [entry], members: {arch: {opcode: [class index]}}}.
const STATUSES = {
  native: 'computed by Ghidra p-code, so its decompiler and emulator see the real operation',
  primitive: 'computed by a named runtime call that our emulator runs; the decompiler sees only the call',
  'partially native': 'native for some of its instructions, opaque for the rest',
  'partially primitive': 'primitive for some of its instructions, opaque for the rest',
  opaque: 'no semantics: Ghidra knows only which registers it reads and writes'};
const ORDER = Object.keys(STATUSES), PARTIAL = {'partially native': 'native', 'partially primitive': 'primitive'};
const EVIDENCE = ['gpu', 'ptxas', 'reference'];   // strongest first
const NAMES = {gpu: 'GPU', ptxas: 'ptxas', reference: 'reference only', none: 'unverified'};
const LETTERS = {gpu: 'G', ptxas: 'P', reference: 'R'};
const MEANS = {
  native: 'Every instruction with this setting is computed in p-code.',
  primitive: 'Every instruction with this setting is modeled, some through a runtime call.',
  'partially native': 'Some instructions with this setting are computed in p-code; the rest, depending on the other modifiers, have no semantics.',
  'partially primitive': 'Some instructions with this setting are modeled through a runtime call; the rest, depending on the other modifiers, have no semantics.',
  opaque: 'No instruction with this setting has semantics (dataflow only).'};
const HINTS = {gpu: 'ran on an SM89 GPU and matched the reference', ptxas: 'PTX with known semantics compiled to SASS by ptxas',
  reference: 'run in our emulator and compared with an independent model of the instruction, not on hardware', none: 'no test ran it'};
const CLASS_HINTS = {gpu: 'at least one of its combinations ran on an SM89 GPU and matched the reference', ptxas: 'ptxas compiled PTX with known semantics to at least one of its combinations',
  reference: 'at least one of its combinations was checked against an independent model in our emulator, none on hardware', none: 'no test ran any of its combinations'};
const RAN = {gpu: 'Ran on the SM89 GPU.', ptxas: 'ptxas emits it for a PTX probe.', reference: 'Checked against the reference only, not on hardware.', none: 'No test ran it.'};
const EXAMPLE = {arch: 'SM89', op: 'F2F', cls: 'f2f_f64_downconvert__RRR_RRR',
  what: 'an instruction that converts a floating-point number to another floating-point format, for example a 64-bit double to a 32-bit float',
  archName: 'the architecture of NVIDIA’s RTX 40-series (Ada) GPUs, the one we test on real hardware',
  // C: the class's name, read off one of its instructions.
  about: `Its name has three parts, each visible in one of its instructions, <code>F2F.F32.F64 R0, R2</code>:</p><ul class="anatomy">
    <li><code>f2f</code>: the instruction, F2F (“float to float”).</li>
    <li><code>f64_downconvert</code>: the variant that converts <i>down</i> from a 64-bit double to a narrower, less precise format, F32 or F16; the instruction’s <code>.F32.F64</code> reads “to F32 from F64”. A sibling class, <code>f2f_f64_upconvert…</code>, converts up to a double.</li>
    <li><code>RRR_RRR</code>: the operand form. Each letter names an operand slot of the instruction’s encoding; F2F uses two, the destination and the source, and here both are registers: <code>R0</code> receives the float, and <code>R2</code> holds the double (with <code>R3</code>, since a double takes two 32-bit registers). Sibling classes take the source as an immediate (<code>RIR</code>, a constant inside the instruction), from constant memory (<code>RCR</code>), from a uniform register (<code>RUR</code>, one register shared by a warp’s 32 threads) or from constant memory through a uniform register (<code>RCXR</code>).</li></ul>`,
  // M: what a modifier is, starting from the same instruction.
  modifiers: 'The <code>.F32.F64</code> in <code>F2F.F32.F64 R0, R2</code> is a <b>modifier</b>: a part of the instruction that selects a variant of the operation, here the pair of formats (F32 from F64, or F16 from F64). Other modifiers select a rounding mode, such as <code>.RZ</code> (round toward zero), or flush results too small for a normal float to zero (<code>.FTZ</code>). Most can be left out, and then take a default (round to nearest, no flushing); the formats cannot, so every F2F names them.',
  // M: reading the class's box with instructions.
  reading: 'Reading the box: <code>F2F.F32.F64 R0, R2</code> is the combination that takes <code>F32.F64</code> on the <code>dstfmt.srcfmt</code> line and the first, default chip on every other line. <code>F2F.F32.F64.RZ R0, R2</code> takes <code>RZ</code> instead on the <code>rnd</code> line, and <code>F2F.FTZ.F32.F64 R0, R2</code> takes <code>FTZ</code> on the <code>ftz</code> line.'};
const CHIPS = 'Each line is a modifier and each chip one of its choices: green, always modeled in p-code (native); yellow, always modeled, through a runtime call (primitive); red, never modeled (opaque); half green and half red, modeled only with some choices of the other modifiers. The “overall” chip after the class name splits the class’s combinations the same way. The letter in a chip is the strongest test that ran that choice (G the GPU, P ptxas, R the reference); no letter, no test ran it. Hover a chip for details; “tested combinations” lists the exact combinations tests ran.';

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
    const verified = (e, n) => n ? `\n   verified: ${tally(e, [...EVIDENCE, 'none'], true)}` : '';
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
    return `<div class="card heat" data-arch="${arch}" role="button" tabindex="0" title="Show only ${arch}; the bottom strip splits its classes by status" style="${heat(percent, 100)}">`
      + `<h2>${arch}</h2><div class="big">${percent}%</div><div class="sub">of opcodes have semantics</div>`
      + `<dl><dt>opcodes</dt><dd>${withSem}/${ops.length}</dd><dt>classes</dt><dd title="${exact(status, 'classes')}">${count(status)}</dd><dt>combinations</dt><dd title="${exact(combos, 'modifier combinations')}">${count(combos)}</dd></dl>`
      + `<div class="sub h">verified classes</div><dl>${EVIDENCE.filter(k => ev[k]).map(k => `<dt>${NAMES[k]}</dt><dd>${ev[k]}</dd>`).join('')}</dl>${strip(status)}</div>`;
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
      const ran = t ? `tests ran ${t.toLocaleString('en')} (${EVIDENCE.filter(k => tested[k]).map(k => `${tested[k]} ${NAMES[k]}`).join(', ')})` : 'no test ran any';
      (mods[m] = mods[m] || []).push(`<span class="chip st-${slug(s)}" title="${esc(m)}=${esc(l)}: ${s}. ${MEANS[s]}\n${total(combos).toLocaleString('en')} ${pl(total(combos), 'combination has', 'combinations have')} it: ${split}; ${ran}.">${esc(l)}${badge(ks)}</span>`);
    }
    for (const [k, fs] of Object.entries(e.forms)) for (const f of fs) (forms[f] = forms[f] || []).push(k);
    const tested = Object.keys(forms).sort();
    return (Object.entries(mods).map(([m, cs]) => `<div class="mod"><code>${esc(m)}</code>${cs.join('')}</div>`).join('') || '<div class="mod">no modifiers</div>')
      + (tested.length ? `<details><summary>${tested.length} tested ${pl(tested.length, 'combination', 'combinations')}</summary><ul>${tested.map(f => `<li><code>${esc(f) || '(defaults)'}</code> ${badge(forms[f])}</li>`).join('')}</ul></details>` : '');
  }

  function verification(part, lead, hints = HINTS) {
    const n = sum(Object.values(part));
    const row = k => `<span class="kind"><span>${NAMES[k]}</span><i class="vbar" style="background:linear-gradient(90deg,var(--${k === 'none' ? 'muted' : 'v-' + k}) ${100 * (part[k] || 0) / n}%,var(--v-none) 0)"></i><span class="n">${(part[k] || 0).toLocaleString('en')} of ${n.toLocaleString('en')}</span><span class="why">${hints[k]}</span></span>`;
    return `<p>${lead(n.toLocaleString('en'))}</p><div class="kinds">${[...EVIDENCE, 'none'].map(row).join('')}</div>`;
  }

  // What op on arch's cell says; `example` works one class through it.
  function explain(op, arch, example) {
    const es = ((members[arch] || {})[op] || []).map(i => classes[i]);
    if (!es.length) return `<h2>${esc(op)} on ${arch}</h2><p>${esc(op)} is not in ${arch}’s machine description.</p>`;
    const [c, m, ce, me] = at(arch, op), ex = example && es.find(e => e.cls === example.cls);
    const split = counts => ORDER.filter(s => counts[s]).map(s => `${counts[s].toLocaleString('en')} ${s}`).join(', ');
    const list = (xs, word = 'or') => xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} ${word} ${xs[xs.length - 1]}`;
    const code = x => `<code>${esc(x)}</code>`, n = x => x.toLocaleString('en');
    const reads = (counts, what) => `so the cell reads <b>${what} ${count(counts)}</b>: ${n(covered(counts) - partial(counts))} fully covered${partial(counts) ? ` and ${n(partial(counts))} partly` : ''}, out of ${n(total(counts))}`;
    let out = `<h2>${example ? 'Example: ' : ''}${esc(op)} on ${arch}</h2>`
      + (example ? `<p>${esc(op)} is ${esc(example.what)}. ${arch} is ${esc(example.archName)}. This is ${esc(op)}’s cell in the table below:</p>` : '')
      + `<div class="sample">${esc(op)}${cell(arch, op, 'div', false)}<span class="legend">C counts classes and M modifier combinations; <b>a+b/n</b> means a are fully covered and b partly covered, out of n.</span></div>`;

    out += `<h3>C: classes</h3><p>NVIDIA’s machine description splits every instruction into <b>classes</b>, one per operand form: which operands are registers, immediates or values read from constant memory.`
      + (ex ? ` For example, ${code(ex.cls)} is one of ${esc(op)}’s ${total(c)} classes. ${example.about}` : '</p>')
      + `<p>A class is <b>covered</b> if our Ghidra module models what at least one of its instructions computes, rather than only which registers it reads and writes, and <b>fully covered</b> if it models all of them. A modeled instruction is <i>native</i> when Ghidra’s p-code computes its result and <i>primitive</i> when a runtime call does; the others are <i>opaque</i>. A class whose instructions are only partly modeled is <i>partially native</i> (or partially primitive); M below shows which instructions. On ${arch}, ${esc(op)}’s ${total(c)} ${pl(total(c), 'class is', 'classes are')} ${split(c)}, ${reads(c, 'C')}. The strip along the cell’s bottom edge draws this split, a partially native class counting half green, half red: ${strip(c, true)}</p>`
      + (covered(c) ? verification(ce, k => `How the ${k} covered ${pl(covered(c), 'class was', 'classes were')} tested. A class counts as tested if a test ran at least one of its instructions (the M bars below show how many); each is counted once, under its strongest test:`, CLASS_HINTS) : '');

    out += `<h3>M: modifier combinations</h3>`
      + (ex ? `<p>${example.modifiers}</p>` : `<p>Within a class, <b>modifiers</b> select variants of the operation, such as a rounding mode (${code('.RZ')}) or flushing results too small for a normal float to zero (${code('.FTZ')}); most can be left out and then take a default.</p>`)
      + `<p>A <b>combination</b> picks one choice for every modifier of a class, so it is one concrete instruction. Only legal combinations count; the machine description forbids some. On ${arch}, ${esc(op)}’s classes have ${n(total(m))} ${pl(total(m), 'combination', 'combinations')}: ${split(m)}, ${reads(m, 'M')}.</p>`;
    if (ex) {
      const mods = {};
      for (const [mod, l, st] of ex.values) if (mod !== null) (mods[mod] = mods[mod] || []).push([l, st]);
      const sizes = Object.values(mods).map(v => v.length), [d] = ex.decides, legal = total(ex.combinations);
      const modeled = d && mods[d].filter(([, st]) => st !== 'opaque').map(([l]) => code(l)), unmodeled = d && mods[d].filter(([, st]) => st === 'opaque').map(([l]) => code(l));
      const ruled = Object.entries(ex.illegal || {}).map(([mod, ls]) => `${code(mod)} ${list(ls.map(code))}`);
      const colors = ex.decides.length === 1 && modeled.length && unmodeled.length
        ? `The colors show what our model covers, and here only ${code(d)} matters: ${list(modeled)} ${pl(modeled.length, 'is', 'are')} green (every instruction with ${pl(modeled.length, 'it', 'them')} is modeled) and ${list(unmodeled)} red (none is). Every other chip is half green, half red: an instruction with that choice is modeled only if it also has ${list(modeled)}.`
        : 'The colors show what our model covers: green, every instruction with that choice; red, none; half green and half red, only some, depending on the other modifiers; yellow, through a runtime call.';
      out += `<p>${code(ex.cls)} has ${sizes.length} modifiers: ${list(Object.entries(mods).map(([mod, v], i) => `${code(mod)} (${v.length}${i ? '' : pl(v.length, ' choice', ' choices')})`), 'and')}`
        + (ruled.length ? ` (the machine description rules out ${list(ruled, 'and')} here)` : '')
        + `, so ${sizes.reduce((a, b) => a * b, 1) === legal ? `${sizes.join(' × ')} = ` : ''}${n(legal)} combinations. Here they are, one line per modifier and one chip per choice:</p>`
        + `<div class="classes">${head(ex) + body(ex)}</div>`
        + `<p>${example.reading}</p>`
        + `<p>${colors} The “overall” chip next to the class name adds this up: of its ${n(legal)} combinations, ${ORDER.filter(s => ex.combinations[s]).map((s, i) => `${n(ex.combinations[s])} ${i ? '' : pl(ex.combinations[s], 'is ', 'are ')}${s}`).join(' and ')}, so the class is ${ex.status}.</p>`
        + `<p>The letter in a chip names the strongest test that ran that choice: G the GPU, P ptxas, R the reference; a chip without a letter was never tested. Hover a chip for details; “tested combinations” lists exactly which combinations the tests ran.</p>`;
    }
    out += covered(m) ? verification(me, k => `How the ${k} covered combinations were tested, each counted once under its strongest test:`) : '';
    if (ex) out += `<h3>Colors</h3><p>Green means native, yellow primitive and red opaque, and they are computed as follows:</p><ul class="anatomy">`
      + `<li>A cell’s background shades from red to green with the share of its classes that are covered, a partly covered class counting half.</li>`
      + `<li>The strip along a cell’s bottom edge splits its classes: each class takes an equal width, in its status’s color, a partially native class half green and half red. ${esc(op)}’s: ${strip(c, true)}</li>`
      + `<li>A chip colors one modifier choice by the class’s combinations that make it: green or yellow if all of them are covered, red if none is, half and half if only some are.</li>`
      + `<li>The “overall” chip splits a class’s combinations in proportion: native green, primitive yellow, opaque red.</li></ul>`
      + `<dl class="key">${['native', 'primitive', 'opaque'].map(st => `<dt><i class="swatch-${st}"></i>${st}</dt><dd>${STATUSES[st]}</dd>`).join('')}</dl>`;
    return out + (ex ? '' : `<h3>${esc(op)}’s classes</h3><p>${CHIPS}</p><div class="classes">${es.map(e => head(e) + body(e)).join('')}</div>`);
  }

  function categoryCell(cells) {
    const present = cells.filter(c => !c.classList.contains('absent')), n = present.filter(c => 'sem' in c.dataset).length;
    return present.length ? [`${n}/${present.length}`, heat(n, present.length)] : null;
  }

  // ---- static parts
  document.getElementById('ev').innerHTML = `<option value="">of any kind or none</option>` + [...EVIDENCE, 'none'].map(k => `<option value="${k}">${NAMES[k]}</option>`).join('');
  const cards = document.getElementById('cards');
  cards.innerHTML = archs.map(card).join('');
  grid.innerHTML = `<thead><tr><th title="C: classes with semantics / classes; M: modifier combinations with semantics / modifier combinations">opcode <small>(C, M)</small></th>`
    + archs.map(a => `<th data-arch="${a}" role="button" tabindex="0" title="Show only opcodes on ${a}">${a}</th>`).join('') + '</tr></thead>'
    + categories.map(([name, ops]) => `<tbody><tr class="category"><th>${esc(name)}</th>${archs.map(() => '<td></td>').join('')}</tr>`
      + ops.map(op => `<tr class="op"><th>${esc(op)}</th>${archs.map(a => cell(a, op)).join('')}</tr>`).join('') + '</tbody>').join('');
  const exampleMembers = (members[EXAMPLE.arch] || {})[EXAMPLE.op];
  if (exampleMembers) document.getElementById('example').innerHTML = explain(EXAMPLE.op, EXAMPLE.arch, EXAMPLE);

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
        c.title = 'opcodes with semantics / opcodes in this category';
      });
    }
    for (const d of grid.querySelectorAll('tr.detail')) d.hidden = d.previousElementSibling.hidden;
    document.getElementById('empty').hidden = !!grid.querySelector('tr.op:not([hidden])');
    const only = document.getElementById('only');
    only.hidden = !state.arch;
    only.innerHTML = state.arch ? `showing ${state.arch} only <button aria-label="Show all architectures">×</button>` : '';
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
