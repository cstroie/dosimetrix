// Study detail for CT, radiography (DX/CR), mammography, nuclear medicine and PET/CT.
// Fluoroscopy and interventional studies keep the dedicated view in study.js (Ka,r curve).
// Shows: facts, dose readouts, exam codes, this study's alerts and data-quality findings,
// and the series / exposures with the DRL that applies to each (exam + age group).

import '../components/dx-data-grid.js';
import { indicationRow } from '../components/indication.js';
import { h, toast, esc, panelHead, pageHead } from '../core/dom.js';
import { hisPanel, weightNote } from '../components/his-panel.js';
import { t } from '../core/i18n.js';
import { num, smart, dap, dapUnit, clock, DAP_UNITS } from '../core/format.js';
import { studyActions } from '../components/study-actions.js';
import { studyHref, setGlandularity, isSample } from '../core/api.js';
import { studyNav } from '../core/study-nav.js';
import '../components/dx-chart.js';
import { baseOption } from '../components/dx-chart.js';

const SYSTEMS = { 'ro-nom-ct': 'Nomenclator CT', 'ro-nom-rx': 'Nomenclator radiologie', 'ro-drl-ms838-2025': 'NRD OMS 838/2025',
  'ec-rp185': 'EU RP 185', 'ro-insp-1255-ct': 'INSP 1255 CT', 'ro-insp-1255-rx': 'INSP 1255 radiologie',
  'ro-insp-1255-xa': 'INSP 1255 intervențional', 'ro-insp-1255-dental': 'INSP 1255 dentar',
  'ro-insp-1255-nm': 'INSP 1255 MN diagnostic', 'ro-insp-1255-nm-hybrid': 'INSP 1255 MN hibride' };

function ratioTag(value, ref) {
  if (value == null || !ref) return '';
  const r = value / ref;
  const cls = r > 1.5 ? 'alert' : r > 1 ? 'warn' : 'ok';
  return h('span', { class: `tag ${cls}`, title: `${smart(value)} / DRL ${smart(ref)}` }, `${num(r, 2)}×`);
}

function readout(label, value, unit, sub, opts = {}) {
  return h('div', { class: `readout ${opts.hot ? 'hot' : ''}` }, h('span', { class: 'l' }, label),
    h('span', { class: `v ${opts.sm ? 'sm' : ''}` }, value, unit ? h('span', { class: 'unit' }, ` ${unit}`) : null), sub ? h('span', { class: 'sub' }, sub) : null);
}

// summaries of one event field: the values present, their range and the highest ratio to a DRL
const vals = (ev, k) => ev.map((e) => e[k]).filter((v) => v != null && v !== '' && !Number.isNaN(v));
function range(ev, k, d = 0) {
  const x = vals(ev, k);
  if (!x.length) return null;
  const lo = Math.min(...x);
  const hi = Math.max(...x);
  return lo === hi ? num(lo, d) : `${num(lo, d)}–${num(hi, d)}`;
}
function topRatio(ev, value, ref) {  // the event furthest above (or nearest to) its DRL
  let best = null;
  for (const e of ev) {
    const v = value(e);
    const r = ref(e);
    if (v == null || !r) continue;
    const q = v / r;
    if (!best || q > best.q) best = { q, e };
  }
  return best;
}
function drlReadout(best, what) {
  if (!best) return readout(`${what} / DRL`, '—', '', 'no DRL for this exam');
  const pct = Math.round(best.q * 100);
  return readout(`${what} / DRL`, `${pct}%`, '', `highest: ${best.e.protocol || best.e.projection || best.e.view || `#${best.e.no ?? ''}`}${best.e.drl?.code ? ` · ${best.e.drl.code}` : ''}`, { hot: best.q > 1 });
}

// ---------------------------------------------------------------- timeline chart
// Like the RF/XA "Running Ka,r" panel: each event as a bar at its time (its dose), and the
// running total as a step line. CT: series DLP (localizers faded) and Σ DLP; DX/CR: DAP
// per exposure and Σ DAP; MG: AGD per view, coloured by breast, and Σ AGD per breast;
// NM/PET: activity per administration (only with two or more). Needs event times.
const TIMELINE = {
  ct: { title: 'Running DLP and series', bar: 'DLP', unit: 'mGy·cm', value: (e) => e.dlp, label: (e) => e.protocol || 'series' },
  rx: { title: 'Running DAP and exposures', bar: 'DAP', unit: null, value: (e) => e.dap_gycm2, label: (e) => [e.projection, e.anatomy].filter(Boolean).join(' · ') || 'exposure' },
  mg: { title: 'Running AGD and views', bar: 'AGD', unit: 'mGy', value: (e) => e.agd_mgy, label: (e) => [e.laterality, e.view].filter(Boolean).join(' ') || 'view' },
  nm: { title: 'Administrations', bar: 'Activity', unit: 'MBq', value: (e) => e.activity_mbq, label: (e) => e.agent || 'administration' },
};

function timelinePanel(kind, events, grid) {
  const cfg = TIMELINE[kind];
  const timed = events.filter((e) => e.time && cfg.value(e) != null);
  if (timed.length < 2) return null;  // one event: the readouts say it all
  timed.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
  const t0 = new Date(timed[0].time).getTime();
  const mins = (e) => (new Date(e.time).getTime() - t0) / 60000;
  const isDap = kind === 'rx';
  const f = isDap ? DAP_UNITS[dapUnit()].factor : 1;
  const unit = cfg.unit || dapUnit();
  let selected = null;
  const option = (th) => {
    const base = baseOption(th);
    const side = (e) => (kind === 'mg' ? (e.laterality === 'L' ? 1 : 0) : 0);
    const colors = [th.devices[0], th.devices[1]];
    // running totals: one line, or one per breast
    const lines = kind === 'mg' ? ['R', 'L'] : [null];
    const runs = lines.map((lat) => {
      let acc = 0;
      const pts = [[0, 0]];
      for (const e of timed) {
        if (lat && e.laterality !== lat) continue;
        pts.push([mins(e), acc]);
        acc += cfg.value(e) * f;
        pts.push([mins(e), acc]);
      }
      return { lat, pts };
    });
    const span = Math.max(mins(timed[timed.length - 1]), 0.5);
    const clockAt = (v) => { const d = new Date(t0 + v * 60000); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
    return {
      ...base,
      legend: { ...base.legend, left: 'center' },
      tooltip: { ...base.tooltip, trigger: 'item', formatter: (p) => {
        if (p.seriesType === 'line') return `${esc(p.seriesName)} ${num(p.value[1], 2)} ${unit}`;
        const e = timed[p.data.i];
        return `#${e.no} ${esc(cfg.label(e))}<br>${clock(e.time)} · ${cfg.bar} ${num(cfg.value(e) * f, 2)} ${unit}${e.localizer ? ' · localizer' : ''}`;
      } },
      grid: { left: 66, right: 70, top: 34, bottom: 30 },
      xAxis: { type: 'value', min: -span * 0.03, max: span * 1.03, ...base._axis, splitLine: { show: false },
        axisLabel: { ...base._axis.axisLabel, formatter: clockAt, hideOverlap: true } },
      yAxis: [
        { type: 'value', name: `${cfg.bar} ${unit}`, nameLocation: 'middle', nameGap: 48, nameRotate: 90, min: 0, splitNumber: 3, ...base._axis },
        { type: 'value', name: `Σ ${cfg.bar}`, nameLocation: 'middle', nameGap: 52, nameRotate: 90, min: 0, splitNumber: 3, position: 'right', ...base._axis, splitLine: { show: false } },
      ],
      series: [
        { name: cfg.bar, type: 'bar', yAxisIndex: 0, barWidth: 6, barGap: '-100%',
          data: timed.map((e, i) => ({ value: [mins(e), cfg.value(e) * f], i,
            itemStyle: { color: colors[side(e)], opacity: e.localizer ? 0.35 : 1,
              borderColor: e.no === selected ? th.text : 'transparent', borderWidth: e.no === selected ? 2 : 0 } })) },
        ...runs.map((r, k) => ({ name: r.lat ? `Σ ${cfg.bar} ${r.lat === 'L' ? 'left' : 'right'}` : `Σ ${cfg.bar}`, type: 'line', yAxisIndex: 1,
          data: r.pts, showSymbol: false, z: 3, lineStyle: { color: r.lat ? colors[k === 0 ? 0 : 1] : th.accent, width: 2, type: r.lat ? 'dashed' : 'solid' },
          itemStyle: { color: r.lat ? colors[k === 0 ? 0 : 1] : th.accent } })),
      ],
    };
  };
  const chart = h('dx-chart', { label: `${cfg.title}: each ${cfg.bar} as a bar at its time, the running total as a line`, height: '280' });
  let bound = false;
  const draw = () => {
    chart.draw(option);
    if (bound || !chart.instance) return;  // on() needs the chart drawn once
    bound = true;
    chart.on('click', (ev) => {
      if (ev.data?.i === undefined) return;
      selected = timed[ev.data.i].no;
      grid.focusById(selected);
      draw();
    });
  };
  queueMicrotask(draw);  // once the panel is in the page
  onUnitChange(grid, draw);
  grid.addEventListener('row-focus', (e) => { if (e.detail?.no !== selected) { selected = e.detail?.no ?? null; draw(); } });
  return h('section', { class: 'panel', 'aria-labelledby': 'h-tl' },
    panelHead(cfg.title, { id: 'h-tl' }, h('span', { class: 'spacer' }),
      h('span', { class: 'muted small' }, kind === 'mg' ? 'colour = breast · click a bar to find it in the table' : kind === 'ct' ? 'faded = localizer · click a bar to find it in the table' : 'click a bar to find it in the table')),
    h('div', { class: 'panel-body' }, chart));
}

// ---------------------------------------------------------------- dose vs DRL (bullet chart)
// For CT and projection X-ray the order in time says little; what matters is each dose
// against its reference level. One row per item, the bar in dose units with a tick at its own
// DRL (red past it); items without a DRL show their value only. CT: the exam's total DLP,
// then each series' DLP against the same exam DRL (they add up to the total). DX/CR: DAP per
// projection, retakes stacked (the DRL applies per projection), then each exposure against
// that DRL. MG: AGD per breast, views stacked (no DRL here: absolute mGy).
function drlPanel(kind, events, grid, unitOf) {
  const rows = [];  // { label, parts: [{ v, e }], ref, unit, d }
  if (kind === 'ct') {
    const series = events.filter((e) => !e.localizer && e.agent === undefined);
    const ref = series.find((e) => e.drl?.dlp)?.drl;
    const total = series.reduce((a, e) => a + (e.dlp || 0), 0);
    if (total) rows.push({ label: 'Exam DLP (total)', parts: series.filter((e) => e.dlp).map((e, i) => ({ v: e.dlp, e, colour: i })), ref: ref?.dlp, unit: 'mGy·cm', d: 0, code: ref?.code });
    // each series: its DLP against the same exam DRL, so the series add up to the total bar.
    // A later series is a repeat only with the same name, the same scan range (±10 %) and the
    // same contrast (header agent): a second series of another name is just the next series.
    const norm = (x) => (x || '').toLowerCase().replace(/\s+/g, ' ').trim();
    const sameRange = (a, b) => (a == null || b == null ? a === b : Math.abs(a - b) <= 0.1 * Math.max(a, b));
    const seenCt = [];
    for (const e of series) {
      if (e.dlp == null) continue;
      const first = seenCt.find((x) => norm(x.protocol) === norm(e.protocol) && sameRange(x.scan_length_mm, e.scan_length_mm)
        && Boolean(x.contrast_agent) === Boolean(e.contrast_agent));
      seenCt.push(e);
      rows.push({ label: `#${e.no} ${e.protocol || 'series'}${first ? ` (repeat of #${first.no})` : ''}`, parts: [{ v: e.dlp, e, colour: rows[0]?.parts.findIndex((p) => p.e === e), repeat: Boolean(first) }],
        ref: ref?.dlp, unit: 'mGy·cm', d: 0,
        extra: e.ctdivol != null ? `CTDIvol ${num(e.ctdivol, 2)} mGy${e.drl?.ctdivol ? ` · DRL ${num(e.drl.ctdivol, 1)} mGy (${Math.round((e.ctdivol / e.drl.ctdivol) * 100)}%)` : ''}` : '' });
    }
    for (const p of rows[0]?.parts || []) {  // a repeat is amber in the total bar too
      p.repeat = rows.some((r, i) => i && r.parts[0].e === p.e && r.parts[0].repeat);
    }
  } else if (kind === 'rx') {
    const by = new Map();
    for (const e of events) {
      if (e.dap_gycm2 == null) continue;
      // a retake = the same projection again (same name: "pulmonar" + "pulmonar"); two
      // projections sharing a DRL (AP and PA chest) stay separate rows
      const name = [e.projection, e.anatomy].filter(Boolean).join(' · ') || e.protocol || `#${e.no}`;
      const key = name.toLowerCase();
      if (!by.has(key)) by.set(key, { label: name, parts: [], ref: e.drl?.dap, unit: unitOf(), d: 2, code: e.drl?.code });
      const g = by.get(key);
      g.parts.push({ v: e.dap_gycm2, e, colour: 0, repeat: g.parts.length > 0 });
    }
    // each projection: its cumulative row (retakes stacked), then every exposure on its own
    for (const r of by.values()) {
      rows.push({ ...r, label: `${r.label} (total)` });
      for (const p of r.parts) {
        rows.push({ label: `#${p.e.no} ${[p.e.projection, p.e.anatomy].filter(Boolean).join(' · ') || p.e.protocol || 'exposure'}${p.repeat ? ' (retake)' : ''}`,
          parts: [p], ref: r.ref, unit: r.unit, d: r.d, code: r.code,
          extra: [p.e.kvp != null ? `${num(p.e.kvp, 0)} kV` : '', p.e.mas != null ? `${num(p.e.mas, 1)} mAs` : '',
            p.e.exposure_index != null ? `EI ${num(p.e.exposure_index, 0)}` : '', p.e.deviation_index != null ? `DI ${num(p.e.deviation_index, 1)}` : ''].filter(Boolean).join(' · ') });
      }
    }
  } else if (kind === 'mg') {
    for (const lat of ['R', 'L']) {
      const parts = events.filter((e) => e.laterality === lat && e.agd_mgy != null).map((e, i) => ({ v: e.agd_mgy, e, colour: i }));
      if (parts.length) rows.push({ label: lat === 'R' ? 'Right breast' : 'Left breast', parts, ref: null, unit: 'mGy AGD', d: 2 });
    }
  }
  if (!rows.length) return null;
  const withRef = rows.some((r) => r.ref);
  const isDap = kind === 'rx';
  const f = isDap ? DAP_UNITS[dapUnit()].factor : 1;
  let selected = null;
  const maxParts = Math.max(...rows.map((r) => r.parts.length));
  const option = (th) => {
    const base = baseOption(th);
    const total = (r) => r.parts.reduce((a, p) => a + p.v, 0);
    // every row in its own dose (a row without a DRL is drawn too); a row with a DRL carries
    // a tick at its DRL, and the tooltip gives the percentage
    const scale = (r, v) => v * (isDap ? f : 1);
    const cats = rows.map((r) => r.label);
    const series = [];
    for (let k = 0; k < maxParts; k += 1) {
      series.push({ type: 'bar', stack: 'v', barMaxWidth: 18, name: k ? `retake ${k}` : 'dose',
        data: rows.map((r, i) => {
          const p = r.parts[k];
          if (!p) return null;
          const over = r.ref && total(r) > r.ref;
          // CT / MG: each series or view its own colour, the same in the total and in its row;
          // X-ray: first exposure accent, retakes amber; past the DRL the whole row is red
          const colour = over ? th.alert : p.repeat ? th.warn
            : kind === 'rx' ? th.accent : th.devices[(p.colour ?? k) % th.devices.length];
          return { value: scale(r, p.v), row: i, part: k,
            itemStyle: { color: colour, opacity: 1,
              borderColor: p.e.no === selected ? th.text : 'transparent', borderWidth: p.e.no === selected ? 2 : 0 } };
        }) });
    }
    if (withRef) series.push({ type: 'scatter', name: 'DRL', silent: true, symbol: 'rect', symbolSize: [2.5, 22], z: 5,
      itemStyle: { color: th.text }, tooltip: { show: false },
      data: rows.map((r, i) => (r.ref ? [scale(r, r.ref), i] : null)) });
    const maxX = Math.max(...rows.map((r) => Math.max(scale(r, total(r)), r.ref ? scale(r, r.ref) : 0))) * 1.08;
    return {
      ...base,
      legend: { show: false },
      tooltip: { ...base.tooltip, trigger: 'item', formatter: (p) => {
        const r = rows[p.data.row];
        const part = r.parts[p.data.part];
        const v = (x) => `${num(x * (isDap ? f : 1), r.d)} ${isDap ? dapUnit() : r.unit}`;
        return `${esc(r.label)}${r.parts.length > 1 ? ` · ${p.data.part ? `retake ${p.data.part}` : 'first'}: ${v(part.v)}` : ''}<br>`
          + `total ${v(total(r))}${r.ref ? ` · DRL ${v(r.ref)} (${Math.round((total(r) / r.ref) * 100)}%)${r.code ? ` · ${esc(r.code)}` : ''}` : ' · no DRL'}`
          + (r.extra ? `<br>${esc(r.extra)}` : '');
      } },
      grid: { left: 150, right: 30, top: 16, bottom: 30 },
      xAxis: { type: 'value', min: 0, max: (v) => Math.max(v.max, maxX), ...base._axis,
        name: `${isDap ? dapUnit() : rows[0].unit}${withRef ? ' · ▮ = DRL' : ''}`, nameLocation: 'middle', nameGap: 22 },
      yAxis: { type: 'category', data: cats, inverse: true, ...base._axis, axisLabel: { ...base._axis.axisLabel, width: 140, overflow: 'truncate' } },
      series,
    };
  };
  const chart = h('dx-chart', { label: 'Dose against the diagnostic reference level, per item', height: String(Math.max(140, 40 + rows.length * 30)) });
  let bound = false;
  const draw = () => {
    chart.draw(option);
    if (bound || !chart.instance) return;
    bound = true;
    chart.on('click', (ev) => {
      const r = rows[ev.data?.row];
      const p = r?.parts[ev.data.part];
      if (!p) return;
      selected = p.e.no;
      grid.focusById(selected);
      draw();
    });
  };
  queueMicrotask(draw);
  onUnitChange(grid, draw);
  grid.addEventListener('row-focus', (e) => { if (e.detail?.no !== selected) { selected = e.detail?.no ?? null; draw(); } });
  const title = { ct: 'Dose vs DRL · exam and series DLP', rx: 'Dose vs DRL · DAP per projection and exposure', mg: 'AGD per breast' }[kind];
  const hint = withRef ? 'tick = DRL · red = above it' : 'no DRL for this exam';
  return h('section', { class: 'panel', 'aria-labelledby': 'h-tl' },
    panelHead(title, { id: 'h-tl' }, h('span', { class: 'spacer' }),
      h('span', { class: 'muted small' }, `${hint}${kind === 'rx' ? ' · amber = retake (same projection again)' : kind === 'ct' ? ' · colour = series · amber = repeat' : ' · colour = view'} · click a bar to find it in the table`)),
    h('div', { class: 'panel-body' }, chart));
}

function ctColumns() {
  return [
    { key: 'no', label: '#', align: 'right', width: '40px' },
    { key: 'protocol', label: 'Series / protocol', render: (e) => h('span', { class: 'sans' }, e.protocol || '—', e.localizer ? h('span', { class: 'tag', style: { 'margin-left': '6px' } }, 'localizer') : null) },
    { key: 'type', label: 'Type', width: '80px' },
    { key: 'ctdivol', label: 'CTDIvol', unit: 'mGy', align: 'right', render: (e) => (e.ctdivol == null ? '' : num(e.ctdivol, 2)) },
    { key: 'phantom_cm', label: 'Ph.', unit: 'cm', align: 'right' },
    { key: 'dlp', label: 'DLP', unit: 'mGy·cm', align: 'right', render: (e) => (e.dlp == null ? '' : num(e.dlp, 1)) },
    { key: 'ssde_mgy', label: 'SSDE', unit: 'mGy', align: 'right', render: (e) => (e.ssde_mgy == null ? '' : h('span', { title: `${e.ssde_source === 'reported' ? 'reported by the scanner' : `calculated (${e.ssde_source})`}${e.wed_cm ? ` · Dw ${num(e.wed_cm, 1)} cm` : ''}` }, num(e.ssde_mgy, 2), e.ssde_source === 'reported' ? '' : '*')) },
    { key: 'scan_length_mm', label: 'Length', unit: 'cm', align: 'right', render: (e) => (e.scan_length_mm == null ? '' : num(e.scan_length_mm / 10, 1)) },
    { key: 'pitch', label: 'Pitch', align: 'right' },
    { key: 'kvp', label: 'kV', align: 'right' },
    { key: 'mas', label: 'mAs', align: 'right' },
    { key: 'exam', label: 'DRL exam', render: (e) => e.exam || '' },
    { key: 'drl_dlp', label: 'DLP / DRL', align: 'right', render: (e) => (e.drl ? ratioTag(e.dlp, e.drl.dlp) : '') },
    { key: 'drl_ctdi', label: 'CTDIvol / DRL', align: 'right', render: (e) => (e.drl ? ratioTag(e.ctdivol, e.drl.ctdivol) : '') },
  ];
}

function rxColumns() {
  return [
    { key: 'no', label: '#', align: 'right', width: '40px' },
    { key: 'time', label: 'Time', width: '80px', render: (e) => (e.time ? clock(e.time) : '') },
    { key: 'protocol', label: 'Protocol', render: (e) => h('span', { class: 'sans' }, e.protocol || '—') },
    { key: 'anatomy', label: 'Anatomy' },
    { key: 'projection', label: 'Proj.', render: (e) => h('span', { title: e.view || 'derived from the protocol' }, e.projection || '') },
    { key: 'kvp', label: 'kV', align: 'right' },
    { key: 'mas', label: 'mAs', align: 'right' },
    { key: 'dap_gycm2', label: 'DAP', unit: dapUnit(), align: 'right', render: (e) => (e.dap_gycm2 == null ? '' : dap(e.dap_gycm2, 3)) },
    { key: 'exposure_index', label: 'EI', align: 'right' },
    { key: 'deviation_index', label: 'DI', align: 'right', render: diCell },
    { key: 'sid_cm', label: 'SID', unit: 'cm', align: 'right' },
    { key: 'exam', label: 'Exam', render: (e) => e.exam || '' },
    { key: 'drl', label: 'DAP / DRL', align: 'right', render: (e) => (e.drl?.dap ? h('span', { title: `${e.drl.code}: DRL ${dap(e.drl.dap)} ${dapUnit()} per projection (retakes add up)` }, ratioTag(e.dap_gycm2, e.drl.dap)) : '') },
  ];
}

// |DI| > 3: the detector dose is outside the target (IEC 62494-1); flagged, not alarmed
function diCell(e) {
  if (e.deviation_index == null) return '';
  return Math.abs(e.deviation_index) > 3 ? h('span', { class: 'tag warn', title: '|DI| > 3: exposure outside the target' }, num(e.deviation_index, 1)) : num(e.deviation_index, 1);
}

function mgColumns() {
  return [
    { key: 'no', label: '#', align: 'right', width: '40px' },
    { key: 'time', label: 'Time', width: '80px', render: (e) => (e.time ? clock(e.time) : '') },
    { key: 'laterality', label: 'Side', width: '50px' },
    { key: 'view', label: 'View', render: (e) => e.projection || e.view || '' },
    { key: 'kvp', label: 'kV', align: 'right' },
    { key: 'mas', label: 'mAs', align: 'right' },
    { key: 'target', label: 'Target / filter', render: (e) => [e.target, e.filter].filter(Boolean).join(' / ') },
    { key: 'thickness_mm', label: 'Thickness', unit: 'mm', align: 'right' },
    { key: 'force_n', label: 'Force', unit: 'N', align: 'right' },
    { key: 'esd_mgy', label: 'ESD', unit: 'mGy', align: 'right', render: (e) => (e.esd_mgy == null ? '' : num(e.esd_mgy, 2)) },
    { key: 'agd_mgy', label: 'AGD', unit: 'mGy', align: 'right', render: (e) => (e.agd_mgy == null ? '' : h('strong', {}, num(e.agd_mgy, 2))) },
  ];
}

function nmColumns() {
  return [
    { key: 'no', label: '#', align: 'right', width: '40px' },
    { key: 'time', label: 'Injection', width: '80px', render: (e) => (e.time ? clock(e.time) : '') },
    { key: 'agent', label: 'Radiopharmaceutical', render: (e) => h('span', { class: 'sans' }, e.agent || '—') },
    { key: 'nuclide', label: 'Nuclide' },
    { key: 'route', label: 'Route' },
    { key: 'activity_mbq', label: 'Activity', unit: 'MBq', align: 'right', render: (e) => (e.activity_mbq == null ? '—' : h('strong', {}, num(e.activity_mbq, 0))) },
    { key: 'per_kg', label: 'MBq/kg', align: 'right', sortable: false, render: (e) => (e.activity_mbq != null && e._kg ? num(e.activity_mbq / e._kg, 2) : '') },
    { key: 'e_msv', label: 'E (est.)', unit: 'mSv', align: 'right', render: (e) => (e.e_msv == null
      ? h('span', { class: 'muted', title: 'No coefficient entered for this nuclide and radiopharmaceutical (dozimetrix effective-dose set)' }, '—')
      : h('span', { title: `${e.e_coefficient} mSv/MBq × ${num(e.activity_mbq, 0)} MBq` }, num(e.e_msv, 2))) },
    { key: 'same_administration_as', label: 'Counted', render: (e) => (e.same_administration_as ? h('a', { href: studyHref({ id: e.same_administration_as }) }, 'on an earlier study') : 'here') },
  ];
}

// Breast glandularity (Order 1255/2016 art. 3): not in DICOM, so typed in; the form's own steps.
function glandularityRow(s) {
  if (s.modality !== 'MG') return [];
  const steps = ['', '0.1', '25', '50', '75', '100'];
  const sel = h('select', { class: 'input no-print', 'aria-label': 'Glandularity', disabled: isSample, onchange: async (e) => {
    try { await setGlandularity(s.id, e.target.value === '' ? null : Number(e.target.value)); toast('Saved'); } catch (err) { toast(err.message); }
  } }, steps.map((v) => h('option', { value: v, selected: String(s.glandularity_pct ?? '') === v }, v === '' ? '—' : `${v} %`)));
  return [h('dt', {}, 'Glandularity'), h('dd', {}, sel)];
}

/** Redraw on a DAP unit change while `el` is in the page (a live re-render replaces it). */
export function onUnitChange(el, fn) {
  const handler = () => (el.isConnected ? fn() : document.removeEventListener('dx-unit-change', handler));
  document.addEventListener('dx-unit-change', handler);
}

export function renderStudy(page, s, { revealButton, ackAlert }) {
  const p = s.patient;
  const T = s.totals;
  const hybrid = (s.modality_label || s.modality) !== s.modality; // PET/CT, SPECT/CT
  const ct = s.modality === 'CT' || hybrid;
  const ev = s.events;
  const identity = h('span', { class: 'mono' }, p.pseudo_id || '—');
  const series = ev.filter((e) => !e.localizer && e.agent === undefined);
  const views = [...new Set(ev.map((e) => e.projection).filter(Boolean))];
  const placeholder = !s.description || /^[-\s.0/]*$/.test(s.description);
  const examName = (s.exams || []).find((x) => x.system.startsWith('ro-nom'))?.meaning;
  const title = placeholder ? (examName || `${s.modality_label || s.modality} study`) : s.description;
  const sumOf = (list, k) => vals(list, k).reduce((a, v) => a + v, 0);
  const ssde = vals(series, 'ssde_mgy');
  const lengthCm = sumOf(series, 'scan_length_mm') / 10;
  const dis = vals(ev, 'deviation_index');
  const diOut = dis.filter((v) => Math.abs(v) > 3).length;
  const forces = range(ev, 'force_n');
  const targets = [...new Set(ev.map((e) => [e.target, e.filter].filter(Boolean).join('/')).filter(Boolean))];
  const agents = [...new Set(ev.map((e) => [e.agent, e.nuclide].filter(Boolean).join(' · ')).filter(Boolean))];
  const eSum = sumOf(ev, 'e_msv');
  const ro = ct ? [
    ...(T.activity_mbq != null ? [readout('Administered activity', num(T.activity_mbq, 0), 'MBq')] : []),
    readout('Total DLP', num(T.dlp_mgycm, 1), 'mGy·cm', `${series.length} series + ${ev.filter((e) => e.localizer).length} localizer`),
    readout('Highest CTDIvol', num(T.ctdivol_max, 2), 'mGy', [...new Set(series.map((e) => e.phantom_cm).filter(Boolean))].map((x) => `${x} cm phantom`).join(' · ')),
    readout('Highest SSDE', ssde.length ? num(Math.max(...ssde), 2) : '—', ssde.length ? 'mGy' : '', ssde.length ? 'size-specific dose estimate' : 'the scanner reported no water-equivalent diameter (Dw): SSDE needs it, weight and height do not give it'),
    drlReadout(topRatio(series, (e) => e.dlp, (e) => e.drl?.dlp), 'DLP'),
    readout('Scan length', lengthCm ? num(lengthCm, 1) : '—', lengthCm ? 'cm' : '', `kV ${range(series, 'kvp') || '—'} · pitch ${range(series, 'pitch', 2) || '—'}`, { sm: true }),
    readout('Contrast', { N: 'native', C: 'contrast', NC: 'native + contrast', MF: 'multiphase' }[s.contrast] || '—'),
  ] : s.modality === 'MG' ? [
    readout('AGD left', T.agd_left_mgy != null ? num(T.agd_left_mgy, 2) : '—', 'mGy', 'per breast: the sum of its views'),
    readout('AGD right', T.agd_right_mgy != null ? num(T.agd_right_mgy, 2) : '—', 'mGy'),
    readout('Highest view AGD', vals(ev, 'agd_mgy').length ? num(Math.max(...vals(ev, 'agd_mgy')), 2) : '—', 'mGy', ev.length ? `of ${ev.length} views` : ''),
    readout('Views', views.length ? views.join(', ') : `${ev.length}`, '', `${ev.length} exposures`),
    readout('Thickness', range(ev, 'thickness_mm') || '—', 'mm', forces ? `compression ${forces} N` : ''),
    readout('kV · target/filter', range(ev, 'kvp') || '—', 'kV', targets.join(', ') || '', { sm: true }),
  ] : ['PT', 'NM'].includes(s.modality) ? [
    readout('Administered activity', T.activity_mbq != null ? num(T.activity_mbq, 0) : '—', 'MBq', T.activity_counted_on ? h('a', { href: studyHref({ id: T.activity_counted_on }) }, 'same administration: counted on the earlier study') : `${ev.length} administration${ev.length === 1 ? '' : 's'}`),
    readout('Per kg', T.activity_mbq != null && p.weight_kg ? num(T.activity_mbq / p.weight_kg, 2) : '—', T.activity_mbq != null && p.weight_kg ? 'MBq/kg' : '', p.weight_kg ? `weight ${num(p.weight_kg, 1)} kg` : 'no weight'),
    readout('Radiopharmaceutical', agents.join(', ') || '—', '', '', { sm: true }),
    readout('Effective dose (est.)', eSum ? num(eSum, 2) : '—', eSum ? 'mSv' : '', eSum ? 'from the activity, by coefficient' : 'no coefficient for this agent'),
  ] : [
    readout('Total DAP', dap(T.dap_gycm2, 3), dapUnit(), `${ev.length} exposures`),
    drlReadout(topRatio(ev, (e) => e.dap_gycm2, (e) => e.drl?.dap), 'DAP'),
    readout('Highest ESD', vals(ev, 'esd_mgy').length ? num(Math.max(...vals(ev, 'esd_mgy')), 2) : '—', vals(ev, 'esd_mgy').length ? 'mGy' : '', 'entrance surface dose'),
    readout('Projections', views.length ? views.join(', ') : '—', '', `SID ${range(ev, 'sid_cm') || '—'} cm`, { sm: true }),
    readout('kV · mAs', range(ev, 'kvp') || '—', 'kV', `mAs ${range(ev, 'mas', 1) || '—'} · total ${num(sumOf(ev, 'mas'), 1)}`, { sm: true }),
    readout('Exposure index', range(ev, 'exposure_index') || '—', '', dis.length ? `DI ${range(ev, 'deviation_index', 1)}${diOut ? ` · ${diOut} outside ±3` : ''}` : 'no deviation index', { hot: diOut > 0 }),
  ];
  // estimates beside the reported values: the CT effective dose (population estimate), the conceptus
  // screening of a pregnant patient, the DAP corrected by the QA register's calibration, centering
  if (T.effective_dose) ro.push(readout('Effective dose (est.)', num(T.effective_dose.msv, 2), 'mSv', `DLP × k, ${T.effective_dose.region.replace('_', '/')}${T.effective_dose.complete ? '' : ' (partly)'}: ${T.effective_dose.note}`));
  if (T.conceptus) ro.push(readout('Conceptus dose (screening)', T.conceptus.mgy ? num(T.conceptus.mgy[1], 1) : '—', T.conceptus.mgy ? 'mGy' : '', T.conceptus.text, { hot: T.conceptus.level === 'document' || T.conceptus.level === 'physicist' }));
  if (T.dap_corrected_gycm2 != null) ro.push(readout('DAP corrected', dap(T.dap_corrected_gycm2, 3), dapUnit(), `× ${T.dap_factor}: DAP meter calibration (QA register)`));
  if (T.centering) ro.push(readout('Centering', `${T.centering.vertical_mm > 0 ? '+' : ''}${num(T.centering.vertical_mm, 0)}`, 'mm', `vertical (+ lower), lateral ${num(T.centering.lateral_mm, 0)} mm${T.centering.method === 'image_center' ? ' · isocentre assumed' : ''}`, { hot: Math.abs(T.centering.vertical_mm) > 20 }));
  // which events this page lists: CT series; NM/PET administrations; MG views; DX/CR exposures
  const kind = ct ? 'ct' : ['PT', 'NM'].includes(s.modality) ? 'nm' : s.modality === 'MG' ? 'mg' : 'rx';
  const KIND = { ct: ['CT series', 'series', 'grid.ct-series', ctColumns], nm: ['Administrations', 'administrations', 'grid.nm-admin', nmColumns],
    mg: ['Views', 'views', 'grid.mg-views', mgColumns], rx: ['Exposures', 'exposures', 'grid.rx-exposures', rxColumns] }[kind];
  ev.forEach((e) => { e._kg = p.weight_kg; });
  // PET/CT, SPECT/CT: the CT series above, the administrations in their own table below
  const admins = ev.filter((e) => e.agent !== undefined);
  let adminGrid = null;
  if (kind === 'ct' && admins.length) {
    adminGrid = h('dx-data-grid', { class: 'study-events', label: 'Administrations', noun: 'administrations', 'storage-key': 'grid.nm-admin' });
    adminGrid.columns = nmColumns();
    adminGrid.rowId = (e) => e.no;
    adminGrid.setState({ sort: 'no', dir: 'asc', page: 1, size: 500 });
    adminGrid.fetcher = async () => ({ total: admins.length, rows: admins });
  }
  const shown = kind === 'ct' ? ev.filter((e) => e.agent === undefined) : ev;
  const grid = h('dx-data-grid', { class: 'study-events', label: KIND[0], noun: KIND[1], 'storage-key': KIND[2] },
    h('button', { slot: 'actions', class: 'btn', type: 'button', onclick: () => exportRows(s, kind) }, 'CSV'));
  grid.columns = KIND[3]();
  grid.rowId = (e) => e.no;
  grid.setState({ sort: 'no', dir: 'asc', page: 1, size: 500 });
  grid.fetcher = async ({ sort, dir }) => {
    const rows = [...shown].sort((a, b) => (a[sort] < b[sort] ? -1 : a[sort] > b[sort] ? 1 : 0) * (dir === 'asc' ? 1 : -1));
    return { total: rows.length, rows };
  };

  const alerts = (s.alerts || []).map((a) => {
    const btn = a.status === 'open' ? h('button', { class: 'btn xs', type: 'button' }, 'Acknowledge') : h('span', { class: 'muted small' }, `acknowledged by ${a.ack_by}`);
    if (a.status === 'open') {
      btn.addEventListener('click', async () => {
        try { await ackAlert(a.id, null); btn.replaceWith(h('span', { class: 'muted small' }, 'acknowledged')); } catch (err) { toast(err.message); }
      });
    }
    return h('div', { class: 'readout' }, h('span', { class: 'l' }, h('span', { class: `tag ${a.level}` }, a.level), ' ', a.name),
      h('span', { class: 'sub' }, a.text, ' ', btn));
  });
  const findings = (s.quality || []).map((f) => h('div', { class: 'readout' },
    h('span', { class: 'l' }, h('span', { class: `tag ${f.severity === 'error' ? 'alert' : 'warn'}` }, f.rule.replaceAll('_', ' '))),
    h('span', { class: 'sub' }, `${f.title}${f.detail ? ` · ${f.detail}` : ''}`)));

  page.replaceChildren(
    pageHead(title, {},
      (s.alerts || []).filter((a) => a.status === 'open').length ? h('span', { class: 'tag alert' }, `${s.alerts.filter((a) => a.status === 'open').length} alerts`) : null,
      h('span', { class: 'spacer' }), studyNav(s.id), studyActions(s), h('button', { class: 'btn no-print', type: 'button', onclick: () => print() }, t('study.print'))),
    h('div', { class: 'detail' },
      h('div', { class: 'side' },
        h('section', { class: 'panel', 'aria-label': 'Patient and exam' },
          h('dl', { class: 'kv' },
            h('dt', {}, 'Accession'), h('dd', { class: 'mono' }, s.accession || '—'),
            h('dt', {}, 'Patient'), h('dd', {}, identity, ' ', revealButton(s, identity)),
            h('dt', {}, 'Age · sex'), h('dd', { class: 'mono' }, `${p.age_display || (p.age != null ? `${p.age} y` : '—')} · ${p.sex || '—'}`),
            h('dt', {}, 'Height · weight'), h('dd', { class: 'mono' }, `${p.height_cm ?? '—'} cm · ${p.weight_kg ?? '—'} kg`, weightNote(p)),
            h('dt', {}, 'Date'), h('dd', { class: 'mono' }, (s.start || '').replace('T', ' ')),
            h('dt', {}, 'Device'), h('dd', {}, s.device.name || '—'),
            h('dt', {}, 'Modality'), h('dd', {}, s.modality_label || s.modality),
            ...glandularityRow(s), ...indicationRow(s))),
        h('section', { class: 'panel', 'aria-labelledby': 'h-ro' }, panelHead(t('study.readouts'), { id: 'h-ro' }), ...ro),
        h('section', { class: 'panel', 'aria-labelledby': 'h-ex' }, panelHead('Exam codes', { id: 'h-ex' }),
          (s.exams || []).length ? s.exams.map((x) => h('div', { class: 'readout' }, h('span', { class: 'l' }, SYSTEMS[x.system] || x.system),
            h('span', { class: 'sub' }, h('span', { class: 'mono' }, x.code), ` · ${x.meaning}`)))
            : h('p', { class: 'muted small', style: { padding: '0 12px' } }, 'No exam code: map this study in Admin › Protocol mappings.')),
        hisPanel(s),
        alerts.length ? h('section', { class: 'panel' }, panelHead('Alerts', {}), ...alerts) : null,
        findings.length ? h('section', { class: 'panel' }, panelHead('Data quality', {}), ...findings) : null),
      h('div', { class: 'main' }, kind === 'nm' ? timelinePanel(kind, shown, grid) : drlPanel(kind, shown, grid, dapUnit), grid, adminGrid,
        h('p', { class: 'muted small' }, ct
          ? 'DRL per series: national OMS 838/2025 DLP for the exam and age group, else RP 185; CTDIvol from RP 185 (head by age, body by weight). Localizers excluded.'
          : kind === 'nm' ? 'Administered activity per injection; a delayed scan of the same injection counts it once, on the first study.'
            : kind === 'mg' ? 'AGD per view as reported by the unit; the per-breast AGD sums the views of that breast (the MGD DRL applies per breast).'
              : 'DRL per projection (OMS 838/2025, by age group). Retakes of the same projection add up to the projection dose.'))));
  grid.reload();  // the grid fetches once it is in the document
  adminGrid?.reload();
  document.title = `${s.accession || s.id} · Dozimetrix`;
  document.querySelector('dx-shell')?.setSub(s.modality || null);
}

function exportRows(s, kind) {
  const cols = {
    ct: ['no', 'protocol', 'type', 'ctdivol', 'phantom_cm', 'dlp', 'ssde_mgy', 'scan_length_mm', 'pitch', 'kvp', 'mas', 'exam'],
    nm: ['no', 'time', 'agent', 'nuclide', 'route', 'activity_mbq', 'same_administration_as'],
    mg: ['no', 'time', 'laterality', 'view', 'kvp', 'mas', 'target', 'filter', 'thickness_mm', 'force_n', 'esd_mgy', 'agd_mgy'],
    rx: ['no', 'time', 'protocol', 'anatomy', 'view', 'kvp', 'mas', 'dap_gycm2', 'exposure_index', 'deviation_index', 'sid_cm', 'exam'],
  }[kind];
  const ct = kind === 'ct';
  const esc = (v) => (v == null ? '' : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v).replace(/^([=+\-@])/, "'$1"));
  const text = [cols.join(',')].concat(s.events.map((e) => cols.map((c) => esc(e[c])).join(','))).join('\r\n');
  h('a', { href: URL.createObjectURL(new Blob(['﻿', text], { type: 'text/csv;charset=utf-8' })), download: `${s.accession || s.id}-${ct ? 'series' : kind === 'nm' ? 'administrations' : 'exposures'}.csv` }).click();
}

