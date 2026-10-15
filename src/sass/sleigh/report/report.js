// Renders the whole report from DATA (base64 gzip JSON written by sass.sleigh.coverage):
// {archs, categories: [[name, [opcode]]], summary: {arch: {opcodes, combination_status, opcode_evidence,
// combination_evidence}}, classes: [entry], members: {arch: {opcode: [class index]}}}.
const STATUSES = {
  native: 'P-code computes the result, so Ghidra’s decompiler and emulator see the real operation.',
  primitive: 'Calls a named runtime operation (<code>sass_prim_*</code>) that our emulator executes; the decompiler sees only a call.',
  'partially native': 'Native for some of its modifier combinations; the rest fall back to the opaque call (its modifiers’ chips say which). Shown half green, half red; its details mark it “overall”.',
  'partially primitive': 'Primitive for some of its encodings, opaque for the rest. Shown half yellow, half red.',
  opaque: 'Calls a generic operation with the machine description’s reads and writes but no semantics: dataflow only.'};
const ORDER = Object.keys(STATUSES), PARTIAL = {'partially native': 'native', 'partially primitive': 'primitive'};
const EVIDENCE = ['gpu', 'ptxas', 'reference'];   // strongest first
const NAMES = {gpu: 'GPU', ptxas: 'ptxas', reference: 'reference only', none: 'unverified'};
const LETTERS = {gpu: 'G', ptxas: 'P', reference: 'R'};
const EVIDENCE_KEY = {
  gpu: 'ran on an SM89 GPU, on the path from the inputs to the checked output of a fixture kernel that passed.',
  ptxas: 'ptxas emits it for an annotated PTX probe (<code>tests/ptx</code>), which shows which PTX operation it implements; values are not checked.',
  reference: 'emulator cases check our semantics against an independent model (<code>tests/refs</code>); it did not run on hardware.',
  none: 'none of these.'};
const MEANS = {
  native: 'Every instruction with this setting is computed in p-code.',
  primitive: 'Every instruction with this setting is modelled, some through a runtime call.',
  'partially native': 'Some instructions with this setting are computed in p-code; the rest, depending on the other modifiers, have no semantics.',
  'partially primitive': 'Some instructions with this setting are modelled through a runtime call; the rest, depending on the other modifiers, have no semantics.',
  opaque: 'No instruction with this setting has semantics (dataflow only).'};
const HINTS = {gpu: 'ran on an SM89 GPU and matched the reference', ptxas: 'PTX with known semantics compiled to SASS by ptxas',
  reference: 'run in our emulator and compared with an independent model of the instruction, not on hardware', none: 'no test ran it'};
const RAN = {gpu: 'Ran on the SM89 GPU.', ptxas: 'ptxas emits it for a PTX probe.', reference: 'Checked against the reference only, not on hardware.', none: 'No test ran it.'};
const EXAMPLE = {arch: 'SM89', op: 'F2F', cls: 'f2f_f64_downconvert__RRR_RRR',
  what: 'an instruction that converts a floating-point number to another floating-point format, for example a 64-bit double to a 32-bit float',
  archName: 'the architecture of NVIDIA’s RTX 40-series (Ada) GPUs, the one we test on real hardware',
  about: 'Its name reads: <code>f2f</code>, the F2F instruction; <code>f64_downconvert</code>, converting a 64-bit double down to a narrower float (F32 or F16); <code>RRR_RRR</code>, its operand form, all registers. For instance <code>F2F.F32.F64.RZ R0, R2</code> converts the double in R2 to a 32-bit float in R0, rounding toward zero (<code>.RZ</code>), and <code>F2F.FTZ.F32.F64 R0, R2</code> also flushes results too small for a float to zero (<code>.FTZ</code>).'};
const CHIPS = 'Each line is a modifier and each chip one of its choices: green, always modelled in p-code (native); yellow, always modelled, through a runtime call (primitive); red, never modelled (opaque); half green and half red, modelled only with some choices of the other modifiers. The “overall” chip after the class name splits the class’s combinations the same way. The letter in a chip is the strongest test that ran that choice (G the GPU, P ptxas, R the reference); no letter, no test ran it. Hover a chip for details; “tested combinations” lists the exact combinations tests ran.';

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
const heat = (n, d) => `--h:${Math.round(120 * n / d)}`;
const gradient = parts => {let at = 0; const t = sum(parts.map(([, n]) => n)) || 1;
  return `linear-gradient(90deg,${parts.map(([c, n]) => `${c} ${100 * at / t}% ${100 * (at += n) / t}%`).join(',')})`};
// Classes by status; a partial class counts half its modelled part, half opaque.
const halves = counts => {const n = {native: counts.native || 0, primitive: counts.primitive || 0, opaque: counts.opaque || 0};
  for (const [p, base] of Object.entries(PARTIAL)) {n[base] += (counts[p] || 0) / 2; n.opaque += (counts[p] || 0) / 2} return n};
const strip = counts => `<i class="strip" style="background:${gradient(Object.entries(halves(counts)).map(([s, n]) => [`var(--${s})`, n]))}"></i>`;

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
      + `<dl><dt>opcodes</dt><dd>${withSem}/${ops.length}</dd><dt>classes</dt><dd>${count(status)}</dd><dt>combinations</dt><dd>${count(combos)}</dd></dl>`
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
    for (const [m, l, s, ks] of e.values) if (m !== null)
      (mods[m] = mods[m] || []).push(`<span class="chip st-${slug(s)}" title="${esc(m)}=${esc(l)}: ${s}. ${MEANS[s]} ${RAN[best(ks)]}">${esc(l)}${badge(ks)}</span>`);
    for (const [k, fs] of Object.entries(e.forms)) for (const f of fs) (forms[f] = forms[f] || []).push(k);
    const tested = Object.keys(forms).sort();
    return (Object.entries(mods).map(([m, cs]) => `<div class="mod"><code>${esc(m)}</code>${cs.join('')}</div>`).join('') || '<div class="mod">no modifiers</div>')
      + (tested.length ? `<details><summary>${tested.length} tested ${pl(tested.length, 'combination', 'combinations')}</summary><ul>${tested.map(f => `<li><code>${esc(f) || '(defaults)'}</code> ${badge(forms[f])}</li>`).join('')}</ul></details>` : '');
  }

  function verification(part, noun, note = '') {
    const n = sum(Object.values(part));
    const row = k => `<span class="kind"><span>${NAMES[k]}</span><i class="vbar" style="background:linear-gradient(90deg,var(--${k === 'none' ? 'muted' : 'v-' + k}) ${100 * (part[k] || 0) / n}%,var(--v-none) 0)"></i><span class="n">${(part[k] || 0).toLocaleString('en')} of ${n.toLocaleString('en')}</span><span class="why">${HINTS[k]}</span></span>`;
    return `<p>How the ${n.toLocaleString('en')} covered ${noun(n)} were tested, each counted once under the strongest test that ran it${note}:</p><div class="kinds">${[...EVIDENCE, 'none'].map(row).join('')}</div>`;
  }

  // What op on arch's cell says; `example` works one class through it.
  function explain(op, arch, example) {
    const es = ((members[arch] || {})[op] || []).map(i => classes[i]);
    if (!es.length) return `<h2>${esc(op)} on ${arch}</h2><p>${esc(op)} is not in ${arch}’s machine description.</p>`;
    const [c, m, ce, me] = at(arch, op), ex = example && es.find(e => e.cls === example.cls);
    const split = counts => ORDER.filter(s => counts[s]).map(s => `${counts[s].toLocaleString('en')} ${s}`).join(', ');
    const list = (xs, word = 'or') => xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} ${word} ${xs[xs.length - 1]}`;
    const code = x => `<code>${esc(x)}</code>`;
    let out = `<h2>${example ? 'Example: ' : ''}${esc(op)} on ${arch}</h2>`
      + (example ? `<p>${esc(op)} is ${esc(example.what)}. ${arch} is ${esc(example.archName)}. This is ${esc(op)}’s cell in the table below:</p>` : '')
      + `<div class="sample">${esc(op)}${cell(arch, op, 'div', false)}<span class="legend">C counts classes and M modifier combinations; <b>a+b/n</b> means a are fully covered and b partly covered, out of n.</span></div>`;
    out += `<h3>C: classes</h3><p>NVIDIA’s machine description splits every instruction into <b>classes</b>, one per operand form: which operands are registers, immediates or values read from constant memory${ex ? ` (${code(ex.cls)} is one of ${esc(op)}’s)` : ''}. A class is <b>covered</b> when our Ghidra module models what at least one of its instructions computes (its semantics), not just which registers it reads and writes, and <b>fully covered</b> when it models all of the class’s legal instructions: <i>native</i> when p-code computes the result, <i>primitive</i> when a runtime call does, <i>opaque</i> (not covered) when neither. A class with only some of its instructions modelled is <i>partially</i> native (see M). ${esc(op)} has ${total(c)} ${pl(total(c), 'class', 'classes')} on ${arch}: ${split(c)}, so the cell reads <b>C ${count(c)}</b>: ${covered(c) - partial(c)} fully covered, ${partial(c)} partly, out of ${total(c)}.</p>`
      + (covered(c) ? verification(ce, n => pl(n, 'class', 'classes'), '. A class counts as tested when a test ran <i>at least one</i> of its combinations, so “GPU” here does not mean all of it ran on the GPU; the M bars below show how much did') : '');
    out += `<h3>M: modifier combinations</h3><p>Within a class, <b>modifiers</b> select variants of the operation, such as a rounding mode (${code('.RZ')}), flushing tiny values to zero (${code('.FTZ')}) or a conversion’s destination format. A <b>combination</b> picks one choice for every modifier, so it is one concrete instruction; only legal combinations count (the machine description forbids some, e.g. converting a 32-bit float to E4M3 in some classes). ${esc(op)}’s classes have ${total(m).toLocaleString('en')} ${pl(total(m), 'combination', 'combinations')} in all: ${split(m)}, so the cell reads <b>M ${count(m)}</b>: ${(covered(m) - partial(m)).toLocaleString('en')} fully covered${partial(m) ? `, ${partial(m).toLocaleString('en')} partly` : ''}, out of ${total(m).toLocaleString('en')}.</p>`;
    if (ex) {
      const mods = {};
      for (const [mod, l, st] of ex.values) if (mod !== null) (mods[mod] = mods[mod] || []).push([l, st]);
      const sizes = Object.values(mods).map(v => v.length), [d] = ex.decides, legal = total(ex.combinations);
      const modelled = d && mods[d].filter(([, st]) => st !== 'opaque').map(([l]) => code(l)), unmodelled = d && mods[d].filter(([, st]) => st === 'opaque').map(([l]) => code(l));
      const decides = ex.decides.length === 1 && modelled.length && unmodelled.length
        ? ` Only ${code(d)} matters to our model: with ${list(modelled)} an instruction is modelled, with ${list(unmodelled)} it is not. The other modifiers’ choices therefore show half green, half red: whether they are modelled depends on ${code(d)}.`
        : ex.decides.length && !covered(ex.combinations) === false && ex.combinations.opaque ? ` Only ${list(ex.decides.map(code), 'and')} decide whether an instruction is modelled.` : '';
      const ruled = Object.entries(ex.illegal || {}).map(([m, ls]) => `${code(m)} ${list(ls.map(code))}`);
      out += `<p>Take ${code(ex.cls)}. ${example.about} `
        + (ruled.length ? `The machine description rules out ${list(ruled, 'and')} in this class, so its ` : 'Its ')
        + `${sizes.length} modifiers have ${list(sizes.map(String), 'and')} legal choices, giving ${legal.toLocaleString('en')} ${pl(legal, 'combination', 'combinations')}${sizes.reduce((a, b) => a * b, 1) === legal ? ` (${sizes.join('×')})` : ''}: ${split(ex.combinations)}.${decides} So the class is ${ex.status} under C.</p>`
        + `<p>${CHIPS}</p><div class="classes">${head(ex) + body(ex)}</div>`;
    }
    out += covered(m) ? verification(me, n => pl(n, 'combination', 'combinations')) : '';
    return out + (ex ? '' : `<h3>${esc(op)}’s classes</h3><p>${CHIPS}</p><div class="classes">${es.map(e => head(e) + body(e)).join('')}</div>`);
  }

  function categoryCell(cells) {
    const present = cells.filter(c => !c.classList.contains('absent')), n = present.filter(c => 'sem' in c.dataset).length;
    return present.length ? [`${n}/${present.length}`, heat(n, present.length)] : null;
  }

  // ---- static parts
  document.getElementById('status-key').innerHTML = ORDER.map(s => `<dt><i class="swatch-${slug(s)}"></i>${s}</dt><dd>${STATUSES[s]}</dd>`).join('');
  document.getElementById('evidence-key').innerHTML = [...EVIDENCE, 'none'].map(k => `<dt><i class="swatch-${k}"></i>${NAMES[k]}${LETTERS[k] ? `<b class="badge ev-${k}">${LETTERS[k]}</b>` : ''}</dt><dd>${EVIDENCE_KEY[k]}</dd>`).join('');
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
