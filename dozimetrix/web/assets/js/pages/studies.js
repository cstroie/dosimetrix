// Study list, per modality group: group tabs, saved-query tabs, typed query bar, server-paged grid and a preview pane.
// The tab owns the modality; the query bar holds the other filters. Tab, query, sort and page
// live in the URL (?mod=&q=&sort=&dir=&page=), so views can be shared. An older link with the
// modality inside q (?q=modality:CT …) still opens on its tab.

import '../components/dx-shell.js';
import '../components/dx-query-bar.js';
import '../components/dx-data-grid.js';
import { h, toast, panelHead } from '../core/dom.js';
import { confirmDialog, promptDialog } from '../core/confirm.js';
import { t } from '../core/i18n.js';
import { num, smart, dap, dapUnit, mmss, dateTime, patientSummary } from '../core/format.js';
import { study, studies, deviceMap, drl, studyHref, studyLabel, exportUrl, modalities, profileOf, isSample, me, dropCache, getPref, putPref, sharedViews, createView, deleteView } from '../core/api.js';
import { prefs } from '../core/prefs.js';
import { splitModality, scopeQuery, retab, withoutAuto, normaliseSaved, scopeWarnings } from '../core/query.js';
import { live } from '../core/live.js';
import { rememberList } from '../core/study-nav.js';

const DEFAULT_MOD = 'CT';
const DEFAULT_Q = 'date:90d origin:local -phantom';
const SAVED_KEY = 'savedQueries.ct';  // the key predates the other modalities; kept for users' saved queries
// One starter query, copied into the user's own saved queries the first time (nothing stored
// yet, and no shared query to start from); from then on it is theirs: deleting it keeps it deleted.
const STARTER = { name: 'CT, last 90 days', q: DEFAULT_Q, mod: DEFAULT_MOD };

const page = document.getElementById('page');
const url = new URL(location.href);
const urlQ = url.searchParams.get('q');
const urlMod = url.searchParams.get('mod');  // a group code, or "all"
const initial = {
  // nothing in the URL: the default view; a q without mod: its modality token picks the tab
  mod: urlMod ? (urlMod.toLowerCase() === 'all' ? null : urlMod.toUpperCase()) : (urlQ == null ? DEFAULT_MOD : undefined),
  q: urlQ ?? DEFAULT_Q,
  sort: url.searchParams.get('sort') || 'date',
  dir: url.searchParams.get('dir') || 'desc',
  page: Number(url.searchParams.get('page')) || 1,
};

function syncUrl(state, mod, q) {
  const u = new URL(location.href);
  u.search = '';
  u.searchParams.set('mod', mod ?? 'all');
  if (q) u.searchParams.set('q', q);
  if (state.sort !== 'date' || state.dir !== 'desc') { u.searchParams.set('sort', state.sort); u.searchParams.set('dir', state.dir); }
  if (state.page > 1) u.searchParams.set('page', state.page);
  history.replaceState(null, '', u);
}

function flagCell(s) {
  const f = s.flags.find((x) => x.level === 'alert') || s.flags[0];
  return f ? h('span', { class: `flagbar ${f.level}`, title: f.text, role: 'img', 'aria-label': f.text }) : '';
}

const range = (a, b, d = 0) => (a == null ? '—' : a === b ? num(a, d) : `${num(a, d)}–${num(b, d)}`);
const diCell = (s) => {
  const out = range(s.di_min, s.di_max, 1);
  const bad = Math.max(Math.abs(s.di_min ?? 0), Math.abs(s.di_max ?? 0)) > 3;
  return bad ? h('span', { class: 'tag warn', title: '|DI| > 3: exposure outside the target' }, out) : out;
};
const agdMax = (s) => (s.agd_left == null && s.agd_right == null ? null : Math.max(s.agd_left ?? 0, s.agd_right ?? 0));
const dapUnitLabel = () => dapUnit();

const COMMON = [
  { key: 'flag', srLabel: 'Flag', sortable: false, width: '22px', render: flagCell },
  { key: 'date', label: 'Date', width: '118px', render: (s) => h('span', { class: 'mono' }, dateTime(s.date).slice(5)) },
  { key: 'device', label: 'Device', render: (s) => s.device_name },
  { key: 'accession', label: 'Accession', width: '140px', render: (s) => h('a', { class: `mono${s.accession ? '' : ' muted'}`, href: studyHref(s), title: s.accession ? null : 'No accession number from the device' }, studyLabel(s)) },
  { key: 'patient', label: 'Patient', width: '130px', render: (s) => h('span', { class: 'mono' }, patientSummary(s.patient)) },
  { key: 'description', label: 'Study description' },
];

// every per-modality column, by the study-row key it shows (profiles list which ones a group gets)
const CATALOGUE = {
  events: { label: 'Ev S/A/L', align: 'right', width: '80px', render: (s) => `${s.events.spiral}/${s.events.axial}/${s.events.localizer}` },
  ctdivol_max: { label: 'CTDIvol max', unit: 'mGy', align: 'right', width: '110px', hidden: true, render: (s) => smart(s.ctdivol_max) },
  dlp_total: { label: 'DLP', unit: 'mGy·cm', align: 'right', width: '110px', render: (s) => (s.dlp_total == null ? '—' : h('strong', {}, num(s.dlp_total))) },
  n_events: { label: 'Events', align: 'right', width: '70px', render: (s) => num(s.n_events) },
  kvp_max: { label: 'kV max', unit: 'kV', align: 'right', width: '80px', sortable: false, render: (s) => num(s.kvp_max) },
  ei_range: { label: 'EI', align: 'right', width: '100px', sortable: false, render: (s) => range(s.ei_min, s.ei_max) },
  di_range: { label: 'DI', align: 'right', width: '90px', sortable: false, render: diCell },
  dap_total: { label: 'DAP', unit: dapUnitLabel(), align: 'right', width: '110px', render: (s) => (s.dap_total == null ? '—' : h('strong', {}, dap(s.dap_total))) },
  fluoro_time: { label: 'Fluoro time', unit: 'm:s', align: 'right', width: '90px', render: (s) => (s.fluoro_time == null ? '—' : mmss(s.fluoro_time)) },
  fluoro_dap: { label: 'DAP fluoro', unit: dapUnitLabel(), align: 'right', width: '100px', hidden: true, render: (s) => (s.fluoro_dap == null ? '—' : dap(s.fluoro_dap)) },
  acq_dap: { label: 'DAP acq.', unit: dapUnitLabel(), align: 'right', width: '100px', hidden: true, render: (s) => (s.acq_dap == null ? '—' : dap(s.acq_dap)) },
  kar_total: { label: 'Ka,r', unit: 'mGy', align: 'right', width: '90px', render: (s) => (s.kar_total == null ? '—' : num(s.kar_total * 1000)) },
  psd: { label: 'PSD (est.)', unit: 'mGy', align: 'right', width: '90px', render: (s) => (s.psd == null ? '—' : num(s.psd)) },
  agd_left: { label: 'AGD L', unit: 'mGy', align: 'right', width: '80px', render: (s) => (s.agd_left == null ? '—' : num(s.agd_left, 2)) },
  agd_right: { label: 'AGD R', unit: 'mGy', align: 'right', width: '80px', render: (s) => (s.agd_right == null ? '—' : num(s.agd_right, 2)) },
  activity: { label: 'Activity', unit: 'MBq', align: 'right', width: '100px', render: (s) => (s.activity == null ? '—' : h('strong', {}, num(s.activity))) },
};

// a mixed list (no single modality): one "main dose" column, each row in its own quantity
function mainDose(s, profiles) {
  const p = profiles.find((x) => x.code === s.group);
  const key = p ? p.main : (s.dlp_total != null ? 'dlp_total' : 'dap_total');
  if (key === 'dlp_total' && s.dlp_total != null) return `${num(s.dlp_total)} mGy·cm`;
  if (key === 'dap_total' && s.dap_total != null) return `${dap(s.dap_total)} ${dapUnit()}`;
  if (key === 'agd_max' && agdMax(s) != null) return `${num(agdMax(s), 2)} mGy AGD`;
  if (key === 'activity' && s.activity != null) return `${num(s.activity)} MBq`;
  return '—';
}

function columnsFor(profile, profiles) {
  if (!profile) {
    return [...COMMON,
      { key: 'modality', label: 'Mod.', width: '60px', sortable: false, render: (s) => s.modality },
      { key: 'main', label: 'Dose', align: 'right', width: '140px', sortable: false, render: (s) => h('span', { class: 'mono' }, mainDose(s, profiles)) }];
  }
  return [...COMMON, ...profile.columns.filter((k) => CATALOGUE[k]).map((k) => ({ key: k, ...CATALOGUE[k] }))];
}

// Saved queries: the shared ones an admin published (for everyone), then the user's own (kept
// on the server per user, mirrored in this browser; browser only with the sample data).
const queries = { shared: [], mine: [], admin: false };

async function loadQueries() {
  const local = prefs.get(SAVED_KEY);
  if (isSample) { queries.mine = local || [{ ...STARTER }]; return; }
  const [shared, mine, user] = await Promise.all([
    sharedViews('study_query').catch(() => []), getPref('studies.saved').catch(() => null), me().catch(() => null)]);
  queries.shared = shared;
  queries.admin = !!user?.roles?.includes('admin');
  if (mine) queries.mine = mine;
  else {
    // first visit since queries moved to the server: take this browser's along
    queries.mine = local || (shared.length ? [] : [{ ...STARTER }]);
    putPref('studies.saved', queries.mine).catch(() => {});
  }
  prefs.set(SAVED_KEY, queries.mine);
}

function saveMine(list) {
  queries.mine = list;
  prefs.set(SAVED_KEY, list);
  if (!isSample) putPref('studies.saved', list).catch((e) => toast(`Query not saved on the server: ${e.message}`));
}

async function share(name, q, mod, fromMine) {
  try {
    const v = await createView('study_query', name, { q, mod: mod ?? null });
    queries.shared = [...queries.shared, v];
    if (fromMine) saveMine(queries.mine.filter((x) => x !== fromMine));  // moved, not copied
    toast(`“${name}” is now shared with everyone`);
  } catch (e) { toast(`Not shared: ${e.message}`); }
}

// The saved-query row shows the queries for the tab open (and the ones for any tab); the
// others wait in a "+N other" menu, and picking one says which tab it opens.
// view = {mod, q, auto, profiles}; onPick(s) applies a saved query {name, q, mod}.
function tabs(view, onPick, onSaved) {
  const { mod, q: currentQ, profiles } = view;
  const tabKey = mod ?? 'all';
  const label = (m) => (m === 'all' ? 'All' : profiles.find((p) => p.code === m)?.label ?? m);
  const nav = h('nav', { class: 'tabs', 'aria-label': 'Saved queries' });
  const all = [...queries.shared.map((v) => ({ ...normaliseSaved({ name: v.name, q: v.value?.q || '', mod: v.value?.mod }, profiles), view: v })),
    ...queries.mine];
  const here = all.filter((s) => s.mod == null || s.mod === tabKey);
  const other = all.filter((s) => !(s.mod == null || s.mod === tabKey));
  const asKept = withoutAuto(currentQ, view.auto);  // filters a tab switch turned off are not part of it
  const match = here.find((s) => s.q === asKept && s.mod === tabKey) || here.find((s) => s.q === asKept);
  const xBtn = (lbl, title, fn) => h('button', { type: 'button', class: 'tab-x', 'aria-label': lbl, title, onclick: fn }, '×');
  const where = (s) => (s.mod == null ? 'any modality' : label(s.mod));
  for (const s of here) {
    const tab = h('button', { type: 'button', 'aria-current': s === match ? 'page' : false,
      title: `${s.view ? 'Shared with everyone' : 'Yours'} · ${where(s)} · ${s.q || '(no filter)'}`, onclick: () => onPick(s) },
    s.view ? h('span', { class: 'muted', 'aria-hidden': 'true' }, '◆ ') : null, s.name);
    const extra = [];
    if (s.view && queries.admin) {
      extra.push(xBtn(`Stop sharing “${s.name}”`, 'Remove this shared query for everyone', async () => {
        if (!(await confirmDialog(`Remove the shared query “${s.name}” for everyone?`, { title: 'Remove shared query', label: 'Remove', danger: true }))) return;
        try { await deleteView(s.view.id); } catch (e) { toast(`Not removed: ${e.message}`); return; }
        queries.shared = queries.shared.filter((v) => v !== s.view);
        toast(`Removed “${s.name}” for everyone`);
        onSaved();
      }));
    } else if (!s.view) {
      if (queries.admin) {
        extra.push(h('button', { type: 'button', class: 'tab-x tab-share', 'aria-label': `Share “${s.name}” with everyone`, title: 'Share with everyone',
          onclick: async () => { await share(s.name, s.q, s.mod, s); onSaved(); } }, '↑'));
      }
      extra.push(xBtn(`Delete “${s.name}”`, 'Delete this saved query', async () => {
        if (!(await confirmDialog(`Delete the saved query “${s.name}”?`, { title: 'Delete saved query', label: 'Delete', danger: true }))) return;
        saveMine(queries.mine.filter((x) => x !== s));
        toast(`Deleted “${s.name}”`);
        onSaved();
      }));
    }
    nav.append(h('span', { class: 'tab-saved' }, tab, ...extra));
  }
  if (other.length) {
    const sel = h('select', { class: 'tab-other', 'aria-label': 'Saved queries for other modalities', onchange: (e) => {
      const s = other[Number(e.target.value)];
      e.target.value = '';
      if (s) onPick(s);
    } }, h('option', { value: '', selected: true }, `+${other.length} other ▾`),
    other.map((s, i) => h('option', { value: String(i) }, `${label(s.mod)} · ${s.view ? '◆ ' : ''}${s.name}`)));
    nav.append(sel);
  }
  // what is kept: the tab's modality, and the filters without those a tab switch turned off
  const keep = () => {
    const q = asKept;
    return { q, mod: mod ?? (splitModality(q, profiles).multi ? 'all' : null) };
  };
  const ask = () => promptDialog(mod ? `The saved query appears as a tab on ${label(mod)}.` : 'The saved query appears as a tab on every modality.',
    { title: 'Name for this query', label: 'Save' });
  nav.append(h('button', { type: 'button', onclick: async () => {
    const name = await ask();
    if (!name) return;
    saveMine([...queries.mine, { name, ...keep() }]);
    toast(isSample ? `Saved “${name}” (this browser only)` : `Saved “${name}” to your account`);
    onSaved();
  } }, match ? '+ Save as…' : '● Unsaved · Save…'));
  if (queries.admin) {
    nav.append(h('button', { type: 'button', title: 'Save this query for everyone', onclick: async () => {
      const name = await ask();
      if (!name) return;
      const k = keep();
      await share(name, k.q, k.mod, null);
      onSaved();
    } }, '+ Save for everyone…'));
  }
  return nav;
}

function preview(s, profile, profiles) {
  if (!s) return h('p', { class: 'muted', style: { padding: '12px' } }, 'Select a study.');
  const flags = s.flags.map((f) => h('span', { class: `tag ${f.level}` }, f.text));
  const kv = [
    ['Accession', s.accession || '—', true], ['Study', s.description], ['Protocol', s.protocol],
    ['Device', s.device_name], ['Patient', `${patientSummary(s.patient)} · ${s.patient.pseudo_id}`, true],
  ];
  const p = profile || profiles.find((x) => x.code === s.group);
  for (const k of p ? p.columns : ['n_events']) {
    const c = CATALOGUE[k];
    if (!c) continue;
    const v = c.render(s);
    kv.push([c.unit ? `${c.label} (${c.unit})` : c.label, typeof v === 'string' ? v : v.textContent, true]);
  }

  // CT series: the study's own events, loaded on selection (the list rows carry only counts)
  const ev = s.events || {};
  const eventsPanel = s.group === 'CT' && (ev.spiral || 0) + (ev.axial || 0) + (ev.localizer || 0) > 0
    ? ctEvents(s) : null;

  return h('div', {},
    h('div', { class: 'row', style: { padding: '12px 12px 0' } }, h('a', { class: 'btn xs', href: studyHref(s) }, `Open ${studyLabel(s)}`)),
    h('dl', { class: 'kv' }, kv.flatMap(([k, v, mono]) => [h('dt', {}, k), h('dd', mono ? { class: 'mono' } : {}, v ?? '—')])),
    eventsPanel,
    flags.length ? h('div', { class: 'row', style: { padding: '0 12px 12px' } }, flags) : null);
}

function groupTabs(profiles, current, onPick) {
  const nav = h('nav', { class: 'tabs', 'aria-label': 'Modality' });
  const mk = (label, code) => h('button', { type: 'button', 'aria-current': current === code ? 'page' : false, onclick: () => onPick(code) }, label);
  nav.append(mk('All', null));
  for (const p of profiles) nav.append(mk(p.label, p.code));
  return nav;
}

async function init() {
  const [devs, drlSet, profiles] = await Promise.all([deviceMap(), drl(), modalities(), loadQueries()]);
  const physical = new Map([...devs.entries()].filter(([id, d]) => id === d.id));
  queries.mine = queries.mine.map((x) => normaliseSaved(x, profiles));  // older ones kept the modality in q
  // the tab owns the modality (mod: a group code, null = All); currentQ holds the other filters
  const first = splitModality(initial.q, profiles, profiles.some((p) => p.code === initial.mod) ? initial.mod : null);
  let mod = first.mod;
  let currentQ = first.q;
  let auto = new Set();  // filters a tab switch turned off (their active form): they come back on a tab they fit
  const eff = () => scopeQuery(mod, currentQ, profiles);  // what the server gets
  const label = (m) => (m == null ? 'All' : profiles.find((p) => p.code === m)?.label ?? m);

  const qb = h('dx-query-bar', { value: currentQ });
  qb.context = { devices: physical, drl: drlSet.ct_dlp_mgycm };
  qb.check = (toks) => scopeWarnings(toks, mod, profiles, physical);  // why a filter may empty this tab
  const tabsHost = h('div');
  const previewBody = h('div');
  let profile = profileOf(profiles, eff());
  const gridKey = (p) => `grid.${p ? p.code.toLowerCase() : 'all'}`;
  const gridLabel = (p) => (p ? `${p.label} studies` : 'Studies');
  const grid = h('dx-data-grid', { label: gridLabel(profile), noun: t('studies.count'), 'storage-key': gridKey(profile) },
    h('button', { slot: 'actions', class: 'btn', type: 'button', onclick: () => exportRows('csv') }, 'CSV'),
    h('button', { slot: 'actions', class: 'btn', type: 'button', onclick: () => exportRows('xlsx') }, 'XLSX'),
    h('a', { slot: 'actions', class: 'btn primary', href: '#', onclick: (e) => { e.currentTarget.href = `charts.html?q=${encodeURIComponent(eff())}`; } }, t('studies.chart')));

  const renderTabs = () => tabsHost.replaceChildren(
    groupTabs(profiles, mod, (code) => go(code, currentQ, { how: 'tab' })),
    tabs({ mod, q: currentQ, auto, profiles }, pickSaved, renderTabs));
  // a saved query for any modality keeps the tab; one for a modality opens its tab
  const pickSaved = (sv) => {
    auto = new Set();  // a saved query is whole: nothing to bring back from earlier tabs
    go(sv.mod == null ? mod : sv.mod === 'all' ? null : sv.mod, sv.q, { how: 'saved', lead: `“${sv.name}” opens on` });
  };
  // columns, their hidden set and the title follow the modality the query is about
  const setProfile = (p) => {
    profile = p;
    const cols = columnsFor(p, profiles);
    const key = gridKey(p);
    if (!prefs.get(`${key}.hidden`)) prefs.set(`${key}.hidden`, cols.filter((c) => c.hidden).map((c) => c.key));
    if (grid.storageId) { grid.storageId = key; grid.hiddenCols = new Set(prefs.get(`${key}.hidden`) || []); }
    grid.setAttribute('storage-key', key);
    grid.setAttribute('label', gridLabel(p));
    grid.table?.setAttribute('aria-label', gridLabel(p));
    grid.columns = cols;
    document.title = `${gridLabel(p)} · Dozimetrix`;
    document.querySelector('dx-shell')?.setSub(p?.label ?? null);  // the crumb follows the query
    if (!cols.some((c) => c.key === grid.state?.sort) && grid.state && grid.state.sort !== 'date') grid.setState({ sort: 'date', dir: 'desc' });
  };
  // Every change of tab or filters comes here. A modality typed in the filters moves to the tab
  // (never broadening: part of a group stays as a filter); filters that do not apply on the
  // new tab are switched off ("~", still visible), and come back on a tab where they apply.
  // how: 'tab' (a tab clicked: narrowing modality filters that do not fit it go off too),
  // 'saved' (a saved query picked), 'typed' (the query bar: what the user just typed stays on,
  // unless its modality token moved the tab).
  function go(nextMod, q, { how = 'typed', lead = null } = {}) {
    const notes = [];
    const offOn = (r) => {
      auto = r.auto;
      if (r.off.length) notes.push(`${r.off.join(', ')} switched off: not for ${label(target)}`);
      if (r.on.length) notes.push(`${r.on.join(', ')} back on`);
      return r.q;
    };
    let target = nextMod;
    let text = how === 'tab' ? offOn(retab(q, target, profiles, auto, { clicked: true })) : q;
    const sp = splitModality(text, profiles, target);
    const moved = sp.mod !== target;
    if (moved && how === 'typed') notes.push(`Modality → ${label(sp.mod)} tab`);
    target = sp.mod;
    text = sp.q;
    if (how === 'saved' || (how === 'typed' && moved)) text = offOn(retab(text, target, profiles, auto));
    if (sp.dropped.length) notes.push(`${sp.dropped.join(', ')} removed: ${label(target)} excludes it already`);
    const tabMoved = target !== mod;
    mod = target;
    currentQ = text;
    if (qb.value !== currentQ) qb.value = currentQ;
    else qb.refresh();  // the notes depend on the tab
    const p = profileOf(profiles, eff());
    if (p !== profile) setProfile(p);
    grid.setState({ page: 1 });
    previewBody.replaceChildren(preview(null));  // the row shown may not be in the new list
    syncUrl(grid.state, mod, currentQ);
    renderTabs();
    grid.reload();
    if (lead && tabMoved) notes.unshift(`${lead} ${label(mod)}`);
    if (notes.length) toast(notes.join(' · '));
  }

  setProfile(profile);
  // new or changed studies of the modality shown (checked every 15 s): the list reloads in place and
  // keeps the row that had the focus (and its preview)
  let seen = null;  // per modality [newest id, last change] when the list was loaded
  let focused = null;
  grid.addEventListener('row-focus', (e) => { focused = grid.rowId(e.detail); });
  live(['studies'], { studies: async (newest) => {
    if (seen === null) { seen = newest; return; }
    const mods = profile?.modalities || Object.keys(newest);
    const moved = mods.some((m) => JSON.stringify(newest[m] ?? null) !== JSON.stringify(seen[m] ?? null));
    seen = newest;
    if (!moved) return;
    const keep = focused;
    dropCache();  // the same list URL would otherwise come back from the read cache
    await grid.reload();
    if (keep != null) grid.focusById(keep);
  } });
  grid.rowId = (s) => s.id ?? s.accession;
  grid.setState({ sort: initial.sort, dir: initial.dir, page: initial.page, size: 50 });
  grid.fetcher = (state) => studies({ q: eff(), ...state }).then((res) => {
    res.rows.forEach((r) => { r._rowClass = r.flags.some((f) => f.level === 'alert') ? 'hot' : ''; });
    rememberList(state, eff(), res.rows, res.total);  // the study pages step through it
    return res;
  });
  grid.addEventListener('state-change', (e) => syncUrl(e.detail, mod, currentQ));
  grid.addEventListener('row-focus', (e) => previewBody.replaceChildren(preview(e.detail, profile, profiles)));
  grid.addEventListener('row-open', (e) => {
    if (e.detail.id != null) location.href = studyHref(e.detail);
    else toast(`Full detail for ${e.detail.accession}: only the XA sample study has a detail page in sample mode.`);
  });
  qb.addEventListener('query-change', (e) => go(mod, e.detail));

  async function exportRows(kind) {
    const server = exportUrl({ q: eff(), format: kind });
    if (server) {  // the server writes every row (CSV or XLSX); the browser just downloads
      h('a', { href: server, download: '' }).click();
      toast('Export started: the file downloads when the server has written it');
      return;
    }
    const res = await studies({ q: eff(), sort: grid.state.sort, dir: grid.state.dir, page: 1, size: 100000 });
    if (kind === 'xlsx') { toast('XLSX export is produced by the server; sample mode offers CSV.'); return; }
    const head = ['date', 'device', 'accession', 'age', 'sex', 'weight_kg', 'description', 'protocol', 'spiral', 'axial', 'localizer', 'ctdivol_max_mgy', 'dlp_total_mgycm'];
    const lines = [head.join(',')].concat(res.rows.map((s) => [s.date, s.device_name, s.accession, s.patient.age ?? '', s.patient.sex, s.patient.weight ?? '',
      s.description, s.protocol, s.events.spiral, s.events.axial, s.events.localizer, s.ctdivol_max, s.dlp_total]
      .map((v) => (/[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v)).join(',')));
    const href = URL.createObjectURL(new Blob(['﻿', lines.join('\r\n')], { type: 'text/csv;charset=utf-8' }));
    h('a', { href, download: 'studies.csv' }).click();
    toast(`Exported ${num(res.rows.length)} rows`);
  }

  page.replaceChildren(
    tabsHost,
    qb,
    h('div', { class: 'split' }, grid,
      h('aside', { class: 'panel preview', 'aria-labelledby': 'h-prev' },
        panelHead(t('studies.preview'), { id: 'h-prev' }),
        previewBody)));
  renderTabs();
  grid.reload();
}

init().catch((err) => page.replaceChildren(h('div', { class: 'state' }, h('span', { class: 'tag alert' }, 'Error'), t('state.error'), h('span', { class: 'muted mono' }, err.message))));

/** The CT events of a study as a bar grid: event, DLP (bar, share of the study's DLP), CTDIvol. */
const eventCache = new Map();
function ctEvents(s) {
  const ref = s.id ?? s.accession;
  const grid = h('div', { class: 'evb', role: 'table', 'aria-label': t('overview.events') },
    h('span', { class: 'muted', style: { 'grid-column': '1 / -1' } }, t('state.loading')));
  if (!eventCache.has(ref)) eventCache.set(ref, study(ref).then((d) => d.events || []));
  eventCache.get(ref).then((events) => {
    const rows = events.filter((e) => e.dlp != null || e.ctdivol != null);
    const total = rows.reduce((a, e) => a + (e.dlp || 0), 0);
    const head = (k, right) => h('span', { class: 'evb-h', role: 'columnheader', style: right ? { 'text-align': 'right' } : {} }, t(k));
    grid.replaceChildren(head('overview.event'), head('overview.dlp'), head('overview.ctdivol', true));
    if (!rows.length) {
      grid.append(h('span', { class: 'muted', style: { 'grid-column': '1 / -1' } }, t('state.empty')));
      return;
    }
    for (const e of rows) {
      const name = e.protocol || (e.localizer ? 'Topogram' : e.type) || `#${e.no}`;
      const pct = total > 0 && e.dlp ? Math.max(1, (100 * e.dlp) / total) : 0;
      grid.append(
        h('span', { class: 'evb-n', title: name, role: 'cell' }, name),
        h('i', { class: e.localizer ? 'loc' : '', style: { width: `${pct}%` }, role: 'cell',
                 title: e.dlp != null ? `${num(e.dlp, 1)} mGy·cm` : '—' }),
        h('span', { class: 'mono', role: 'cell', style: { 'text-align': 'right' } },
          e.ctdivol != null ? num(e.ctdivol, 2) : '—'));
    }
  }).catch(() => {
    eventCache.delete(ref);
    grid.replaceChildren(h('span', { class: 'muted', style: { 'grid-column': '1 / -1' } }, t('state.error')));
  });
  return grid;
}
