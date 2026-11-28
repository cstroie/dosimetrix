// Data access. Pages call these functions only, never fetch() directly.
//
// Mode is chosen by <html data-api="...">:
//   "sample" (default) → static JSON in data/sample/, filtered and paged in the browser;
//   anything else      → treated as the base URL of the Dozimetrix REST API (planned endpoints
//                        are noted per function; the response shapes are the same).

import { parse, matchStudy, active, groupOf } from './query.js';

const MODE = document.documentElement.dataset.api || 'sample';
const SAMPLE = new URL('../../../data/sample/', import.meta.url);
// Reads are remembered by URL, so several panels asking the same thing share one request. An entry
// lives CACHE_TTL_MS (a page left open must not keep showing old rows) and the map holds at most
// CACHE_MAX (the chart rows of one query are tens of thousands of objects); a write drops all.
const CACHE_TTL_MS = 5 * 60 * 1000;
const CACHE_MAX = 40;
const cache = new Map();  // url -> { at, promise }, oldest first

function cached(url) {
  const hit = cache.get(url);
  if (hit && Date.now() - hit.at < CACHE_TTL_MS) {
    cache.delete(url);
    cache.set(url, hit);  // most recently used last
    return hit.promise;
  }
  return null;
}

function remember(url, promise) {
  cache.delete(url);
  cache.set(url, { at: Date.now(), promise });
  while (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value);
}

async function getJSON(url) {
  let promise = cached(url);
  if (!promise) {
    promise = fetch(url, { headers: { Accept: 'application/json' } }).then(async (r) => {
      if (!r.ok) {
        // the server's own words ("query not understood: …") rather than the bare status
        const body = await r.json().catch(() => ({}));
        const said = body.error || (typeof body.detail === 'string' ? body.detail : null);
        throw new Error(said || `${r.status} ${r.statusText} — ${url}`);
      }
      return r.json();
    });
    remember(url, promise);
  }
  try {
    return await promise;
  } catch (e) {
    if (cache.get(url)?.promise === promise) cache.delete(url);
    throw e;
  }
}

const sample = (name) => getJSON(new URL(name, SAMPLE).href);
const remote = (path, params = {}) => {
  const u = new URL(path, MODE);
  // a query is sent without its switched-off ("~") filters; /api/lookup's q is free text
  const isQuery = (k) => k === 'q' && path !== '/api/lookup';
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') u.searchParams.set(k, isQuery(k) ? active(v) : v);
  return getJSON(u.href);
};

export const isSample = MODE === 'sample';

/** Forget the cached reads: a live update says the server has newer data than the ones held. */
export const dropCache = () => cache.clear();

/** Writes (API mode only). Every successful write drops the read cache. */
async function send(method, path, body) {
  const r = await fetch(new URL(path, MODE).href, {
    method, headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  cache.clear();
  return data;
}

/** GET /api/me/prefs/{key} → the value this user stored on the server (never cached) */
export async function getPref(key) {
  if (isSample) return null;
  const r = await fetch(new URL(`/api/me/prefs/${encodeURIComponent(key)}`, MODE).href, { headers: { Accept: 'application/json' } });
  if (!r.ok) return null;
  return (await r.json()).value;
}
/** PUT /api/me/prefs/{key}  {value} */
export const putPref = (key, value) => send('PUT', `/api/me/prefs/${encodeURIComponent(key)}`, { value });

/** GET /api/views?kind= → the views an admin shared with everyone ([] with the sample data) */
export async function sharedViews(kind) {
  if (isSample) return [];
  return (await remote('/api/views', { kind })).views;
}
/** POST /api/views {kind, name, value} (admin, audited) */
export const createView = (kind, name, value) => send('POST', '/api/views', { kind, name, value });
/** PUT /api/views/{id} {name?, value?, position?} (admin, audited) */
export const updateView = (id, patch) => send('PUT', `/api/views/${id}`, patch);
/** DELETE /api/views/{id} (admin, audited) */
export const deleteView = (id) => send('DELETE', `/api/views/${id}`);
/** GET /api/page-defaults → {drl, quality, alerts}: the filters an admin set as each page's default */
export async function pageDefaults() {
  if (isSample) return {};
  try { return await remote('/api/page-defaults'); } catch { return {}; }
}
/** PUT /api/page-defaults/{page} {field: value} (admin, audited) */
export const putPageDefaults = (page, value) => send('PUT', `/api/page-defaults/${page}`, value);

/** PATCH /api/devices/{id}  {display_name?, modality_override? ('auto' clears), origin?} */
export const updateDevice = (id, patch) => send('PATCH', `/api/devices/${id}`, patch);
/** POST /api/devices/merge  {record_ids, survivor_id, display_name?} */
export const mergeDevices = (recordIds, survivorId, displayName) =>
  send('POST', '/api/devices/merge', { record_ids: recordIds, survivor_id: survivorId, display_name: displayName });
/** POST /api/devices/{id}/split */
export const splitDevice = (id) => send('POST', `/api/devices/${id}/split`);
/** POST /api/devices/merge-suggestions/dismiss  {record_ids} */
export const dismissMerge = (recordIds) => send('POST', '/api/devices/merge-suggestions/dismiss', { record_ids: recordIds });

/** POST /api/import/sizes  raw CSV body; a dry run unless apply (admin, audited). */
export async function importSizes(file, apply = false) {
  const r = await fetch(new URL(`/api/import/sizes${apply ? '?apply=1' : ''}`, MODE).href, {
    method: 'POST', headers: { 'Content-Type': 'text/csv', Accept: 'application/json' }, body: file,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || `${r.status} ${r.statusText}`);
  cache.clear();
  return data;
}

/** "Now" for relative times: the sample data was generated at a fixed moment. */
export async function now() {
  if (!isSample) return new Date().toISOString().slice(0, 16);
  return (await sample('overview.json')).generated;
}

/** GET /api/me → { username, name, roles } (null with the sample data) */
export async function me() {
  return isSample ? null : remote('/api/me');
}

/** True for an admin, and with the sample data (no accounts): the pages show their write controls. */
export async function isAdmin() {
  const u = await me().catch(() => null);
  return !u || u.roles.includes('admin');
}

/** POST /api/logout, then the login page. */
export async function logout() {
  await fetch(new URL('/api/logout', MODE).href, { method: 'POST' }).catch(() => {});
  cache.clear();
  location.href = '/login';
}

/** GET /api/overview */
export async function overview() {
  return isSample ? sample('overview.json') : remote('/api/overview');
}

/** GET /api/devices → { records: [...], merge_suggestions: [...] } */
export async function devices() {
  return isSample ? sample('devices.json') : remote('/api/devices');
}

/** Map of physical device id → canonical record (aliases folded in). */
export async function deviceMap() {
  const { records } = await devices();
  const byId = new Map(records.map((r) => [r.id, r]));
  const out = new Map();
  for (const r of records) out.set(r.id, byId.get(r.physical_id) || r);
  return out;
}

export async function physicalDevices() {
  const { records } = await devices();
  return records.filter((r) => r.id === r.physical_id);
}

// Sample mode has no server: the same profiles as dozimetrix/modality_profiles.py.
const SAMPLE_PROFILES = [
  { code: 'CT', label: 'CT', modalities: ['CT'], columns: ['events', 'ctdivol_max', 'dlp_total'], main: 'dlp_total' },
  { code: 'DX', label: 'DX/CR', modalities: ['DX', 'CR'], columns: ['n_events', 'kvp_max', 'ei_range', 'di_range', 'dap_total'], main: 'dap_total' },
  { code: 'RF', label: 'RF', modalities: ['RF'], columns: ['n_events', 'fluoro_time', 'fluoro_dap', 'acq_dap', 'kar_total', 'dap_total'], main: 'dap_total' },
  { code: 'XA', label: 'XA', modalities: ['XA'], columns: ['n_events', 'fluoro_time', 'kar_total', 'psd', 'dap_total'], main: 'dap_total' },
  { code: 'MG', label: 'MG', modalities: ['MG'], columns: ['n_events', 'agd_left', 'agd_right'], main: 'agd_max' },
  { code: 'NM', label: 'NM', modalities: ['NM'], columns: ['activity', 'dlp_total'], main: 'activity' },
  { code: 'PT', label: 'PT', modalities: ['PT'], columns: ['activity', 'dlp_total'], main: 'activity' },
];

/** GET /api/modalities → one profile per modality group (columns, metrics, DRL quantities) */
export async function modalities() {
  return isSample ? SAMPLE_PROFILES : remote('/api/modalities');
}

/** The profile a query is about: its modality: token names one group only, else null. */
export function profileOf(profiles, q) {
  return groupOf(q, profiles);  // every active modality token counts, intersected
}

/** GET /api/drl → local DRL set */
export async function drl() {
  return isSample ? sample('drl.json') : remote('/api/drl');
}

const SORTERS = {
  date: (s) => s.date,
  device: (s, ctx) => ctx.devices.get(s.device_id)?.display_name || '',
  accession: (s) => s.accession,
  patient: (s) => s.patient.age ?? -1,
  description: (s) => s.description,
  events: (s) => s.events.spiral + s.events.axial + s.events.localizer,
  dlp_total: (s) => s.dlp_total,
  ctdivol_max: (s) => s.ctdivol_max,
};

/**
 * GET /api/studies?q=&sort=&dir=&page=&size=
 * → { total, rows: [study + device_name] }
 */
export async function studies({ q = '', sort = 'date', dir = 'desc', page = 1, size = 50 } = {}) {
  if (!isSample) return remote('/api/studies', { q, sort, dir, page, size });
  const [all, devs, n] = await Promise.all([sample('ct-studies.json'), deviceMap(), now()]);
  const tokens = parse(q);
  const ctx = { devices: devs, now: n };
  const rows = all.filter((s) => matchStudy(s, tokens, ctx));
  const key = SORTERS[sort] || SORTERS.date;
  const sgn = dir === 'asc' ? 1 : -1;
  rows.sort((a, b) => {
    const x = key(a, ctx);
    const y = key(b, ctx);
    return (x < y ? -1 : x > y ? 1 : 0) * sgn;
  });
  const start = (page - 1) * size;
  return {
    total: rows.length,
    rows: rows.slice(start, start + size).map((s) => ({ ...s, device_name: devs.get(s.device_id)?.display_name || '—' })),
  };
}

/**
 * GET /api/studies/{id}. The API addresses studies by internal id: accession numbers can be
 * missing, hashed or shared. Sample mode keeps using the accession (one XA showcase).
 */
export async function study(ref) {
  if (!isSample) return remote(`/api/studies/${encodeURIComponent(ref)}`);
  return sample('study-A2026112100123.json');
}

/** Link to a study's detail page: by id when the API provides one, else by accession. */
/** How a study is named in lists: its accession, else its Dozimetrix id (some devices,
 *  e.g. the Artis and the Luminos, send no accession number). */
export function studyLabel(s) {
  return s.accession || (s.id != null ? `study ${s.id}` : '—');
}

export function studyHref(s) {
  return s.id != null ? `study.html?id=${s.id}` : `study.html?acc=${encodeURIComponent(s.accession)}`;
}

/** Quick lookup for the command palette and the overview: accession prefix. */
export async function lookup(text, limit = 6) {
  const t = text.trim().toUpperCase();
  if (t.length < 3) return [];
  if (!isSample) return remote('/api/lookup', { q: t, limit });
  const [all, devs, xa] = await Promise.all([sample('ct-studies.json'), deviceMap(), study('A2026112100123')]);
  const list = [{ accession: xa.accession, modality: 'XA', description: xa.description, device_name: xa.device.name, date: xa.start, dose: `${Math.round(xa.totals.dap_gycm2 * 1000)} mGy·cm²` }];
  for (const s of all) list.push({ accession: s.accession, modality: 'CT', description: s.description, device_name: devs.get(s.device_id)?.display_name, date: s.date, dose: `${s.dlp_total} mGy·cm` });
  return list.filter((s) => s.accession.startsWith(t)).slice(0, limit);
}

/**
 * CT acquisitions for statistics.
 * GET /api/stats/ct/acquisitions?q= (the real API will return aggregates; sample returns rows)
 * → [{protocol, device_id, device, ctdivol, dlp, weight, time}]
 */
export async function ctAcquisitions(q = '') {
  if (!isSample) return remote('/api/stats/ct/acquisitions', { q });
  const [rows, devs, n] = await Promise.all([sample('ct-acquisitions.json'), deviceMap(), now()]);
  const tokens = parse(q).filter((t) => !t.off && ['date', 'device', 'proto', 'origin', 'weight'].includes(t.key));
  const ctx = { devices: devs, now: n };
  return rows
    .map((r) => ({ protocol: r.p, device_id: r.d, device: devs.get(r.d)?.display_name, ctdivol: r.c, dlp: r.l, weight: r.w, time: r.t }))
    .filter((r) => matchStudy({
      description: '', protocol: r.protocol, date: r.time, device_id: r.device_id, test: false, flags: [],
      patient: { age: null, weight: r.weight }, events: { spiral: 1, axial: 0, localizer: 0 }, dlp_total: r.dlp, ctdivol_max: r.ctdivol,
    }, tokens, ctx));
}

/**
 * GET /api/stats/{group}/rows?q= → chart rows of one modality group (CT: acquisitions;
 * DX/CR, MG: exposures; RF, XA, NM, PT: studies). Sample mode has CT data only.
 */
export async function groupRows(group, q = '') {
  if (group === 'CT') return ctAcquisitions(q);
  return isSample ? [] : remote(`/api/stats/${group}/rows`, { q });
}

/* ---------------- statistics, reference levels, exam classification (server only) ---------------- */

function serverOnly(what) {
  return Promise.reject(new Error(`${what} needs the Dozimetrix server (sample mode has no data for it)`));
}

/**
 * GET /api/stats?q=&metric=&by=&system=&exam=&age_scheme=&weight_scheme=&bins=&min_n=
 * → { metric, unit, label, level, group_by, n, n_studies, overall, bins,
 *     groups: [{ key, label, n, n_studies, mean, sd, min, p25, median, p75, max, hist? }] }
 */
export function stats(params = {}) {
  return isSample ? serverOnly('Statistics') : remote('/api/stats', params);
}

/** GET /api/charts/size-bands?device=&kind=weight|age&q= → radiography DAP by the size band in the protocol name */
export function sizeBands(params = {}) {
  return isSample ? serverOnly('Dose by size band') : remote('/api/charts/size-bands', params);
}

/** GET /api/drl/sets → [{ key, name, source, notes, values }] */
export function drlSets() {
  return isSample ? serverOnly('Reference levels') : remote('/api/drl/sets');
}

/** GET /api/drl/sets/{key} → { key, name, source, notes, values: [{ exam, exam_meaning, modality,
 *  scheme, band, band_label, quantity, value, unit, applies_to }] } */
/** Report specs (docs/specs.md): GET /api/exports → { specs, problems, plugins };
 *  GET /api/exports/{id}?format=&<params> runs one (json for the preview, or a file). */
export const exportList = () => (isSample ? serverOnly('Reports') : remote('/api/exports'));
export const exportRun = (id, params) => (isSample ? serverOnly('Reports') : remote(`/api/exports/${encodeURIComponent(id)}`, params));
export function specUrl(id, params, format) {
  if (isSample) return null;
  const u = new URL(`/api/exports/${encodeURIComponent(id)}`, MODE);
  for (const [k, v] of Object.entries({ ...params, format })) if (v !== '' && v != null) u.searchParams.set(k, String(v));
  return u.href;
}

/** Plan of optimisation measures (art. 7(2)): GET /api/actions?year=&quarter=; writes need exporter. */
export const actionList = (year, quarter) => (isSample ? serverOnly('Reports') : remote('/api/actions', { year, quarter }));
export const addAction = (a) => send('POST', '/api/actions', a);
export const editAction = (id, patch) => send('PUT', `/api/actions/${id}`, patch);
export const deleteAction = (id) => send('DELETE', `/api/actions/${id}`);
/** POST /api/deadlines/{id}/done: a person says the report was sent (nothing is sent by Dozimetrix). */
export const deadlineDone = (id) => send('POST', `/api/deadlines/${encodeURIComponent(id)}/done`);

/** PUT /api/studies/{id}/glandularity {percent|null} (admin): mammography, typed in. */
export const setGlandularity = (id, percent) => send('PUT', `/api/studies/${id}/glandularity`, { percent });
/** PUT /api/studies/{id}/indication {code|null} (admin); PUT /api/indications/mappings. */
export const setStudyIndication = (id, code) => send('PUT', `/api/studies/${id}/indication`, { code });
export const decideIndication = (text, code) => send('PUT', '/api/indications/mappings', { text, code });
export function indicationReview() {
  return isSample ? serverOnly('Indications') : remote('/api/indications/review');
}

/** Central survey collection (aggregate.py). */
export async function uploadSurvey(file) {
  const r = await fetch(new URL(`/api/survey/uploads?filename=${encodeURIComponent(file.name)}`, MODE).href, {
    method: 'POST', headers: { 'Content-Type': 'text/csv', Accept: 'application/json' }, body: file,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  cache.clear();
  return data;
}
export function surveyUploads() { return isSample ? serverOnly('Central survey') : remote('/api/survey/uploads'); }
export const deleteSurveyUpload = (id) => send('DELETE', `/api/survey/uploads/${id}`);
export const nameInstitution = (code, name) => send('PUT', `/api/survey/institutions/${encodeURIComponent(code)}`, { name });
export function surveyNational(params) { return isSample ? serverOnly('Central survey') : remote('/api/survey/national', params); }
export function surveyNationalUrl(params) {
  if (isSample) return null;
  const u = new URL('/api/survey/national', MODE);
  for (const [k, v] of Object.entries({ ...params, format: 'xlsx' })) u.searchParams.set(k, String(v));
  return u.href;
}

/** PUT /api/me/password {current, new}: one's own password. */
export const changePassword = (current, next) => send('PUT', '/api/me/password', { current, new: next });

/** GET /api/protocols/compare (protocols.py): the protocols of one CT region side by side. */
export function protocolsCompare(params) {
  return isSample ? serverOnly('Protocol comparison') : remote('/api/protocols/compare', params);
}

/** Local DRLs (localdrl.py): proposal, adoption (admin), review (json / xlsx), deletion. */
export function localDrlPropose(params) {
  return isSample ? serverOnly('Local DRLs') : remote('/api/drl/local/propose', params);
}
export const adoptLocalDrl = (body) => send('POST', '/api/drl/local', body);
export const deleteDrlSet = (key) => send('DELETE', `/api/drl/sets/${encodeURIComponent(key)}`);
export function localDrlReview(params) {
  return isSample ? serverOnly('Local DRLs') : remote('/api/drl/local/review', params);
}
export function localDrlReviewUrl(params) {
  if (isSample) return null;
  const u = new URL('/api/drl/local/review', MODE);
  for (const [k, v] of Object.entries({ ...params, q: active(params.q ?? ''), format: 'xlsx' })) if (v != null && v !== '') u.searchParams.set(k, String(v));
  return u.href;
}

export function drlSet(key) {
  return isSample ? serverOnly('Reference levels') : remote(`/api/drl/sets/${encodeURIComponent(key)}`);
}

/**
 * GET /api/drl/compare?q=&set=&by=facility|device&min_n=
 * → { set, name, source, notes, by, min_n, rows: [{ exam, exam_meaning, band, band_label,
 *     quantity, applies_to, reference, unit, status, reason?, groups: [{ device, device_name,
 *     n, median, p25, p75, mean, ratio, status }] }] }
 */
export function drlCompare(params = {}) {
  return isSample ? serverOnly('Reference levels') : remote('/api/drl/compare', params);
}

/** GET /api/classification/summary → [{ family, modalities, studies, classified, share }] */
export function classificationSummary() {
  return isSample ? serverOnly('Exam mapping') : remote('/api/classification/summary');
}

/** GET /api/classification/unclassified?family=rx|rf|ct&limit= → [{ texts, exposures?, studies }] */
export function unclassified(family, limit = 100) {
  return isSample ? serverOnly('Exam mapping') : remote('/api/classification/unclassified', { family, limit });
}

/** GET /api/classification/mappings → [{ id, family, field, text, system, code, meaning }] */
export function mappings() {
  return isSample ? serverOnly('Exam mapping') : remote('/api/classification/mappings');
}

/** GET /api/codes/{system} → [{ code, meaning, chapter, modality }] */
export function codes(system) {
  return isSample ? serverOnly('Exam mapping') : remote(`/api/codes/${encodeURIComponent(system)}`);
}

/** PUT /api/classification/mappings {family, field, text, code|null} (admin) */
export const decideMapping = (family, field, text, code) =>
  send('PUT', '/api/classification/mappings', { family, field, text, code });

/**
 * Download link for GET /api/export?q=&level=study|event&format=csv|xlsx&pid=
 * (exporter or admin role; pid=1 needs the pid role; every export is audit-logged).
 */
/** GET /api/studies/{id}/rdsr: the study's dose SR (IHE REM) as a file; pseudonymised. */
export function rdsrUrl(id) {
  return isSample ? null : new URL(`/api/studies/${id}/rdsr`, MODE).href;
}

export function exportUrl({ q = '', level = 'study', format = 'csv', pid = false } = {}) {
  if (isSample) return null;
  const u = new URL('/api/export', MODE);
  u.searchParams.set('q', active(q));
  u.searchParams.set('level', level);
  u.searchParams.set('format', format);
  if (pid) u.searchParams.set('pid', '1');
  return u.href;
}

/** Download links: GET /api/drl/summary (ICRP 135) and /api/drl/dataset (exporter role, audited). */
export function drlDownloadUrl(kind, { q = '', set = '', min_n = 20, format = 'xlsx' } = {}) {
  if (isSample) return null;
  const u = new URL(kind === 'dataset' ? '/api/drl/dataset' : '/api/drl/summary', MODE);
  u.searchParams.set('q', active(q));
  u.searchParams.set('format', format);
  if (kind !== 'dataset') { u.searchParams.set('set', set); u.searchParams.set('min_n', String(min_n)); }
  return u.href;
}

/** GET /api/reports/insp1255?year=&quarter=&q= → { period, notes, count_bands, mean_bands, sheets } */
export function inspReport(params) {
  return isSample ? serverOnly('Reports') : remote('/api/reports/insp1255', params);
}

/** GET /api/reports/grid?year=&quarter=&q= → { grid, start, end, min_n, notes, rows: [{ exam,
 *  exam_meaning, parameter (age|weight), group, group_label, quantity, unit, n, n_studies,
 *  excluded, mean, median, p75, edrl, ratio, status }] } (second grid: ICRP 135 / RP 185) */
export function gridReport(params) {
  return isSample ? serverOnly('Reports') : remote('/api/reports/grid', params);
}

export function gridReportUrl({ year, quarter, q = 'origin:local', estimated = false }) {
  if (isSample) return null;
  const u = new URL('/api/reports/grid', MODE);
  for (const [k, v] of Object.entries({ year, quarter, q: active(q), estimated, format: 'xlsx' })) u.searchParams.set(k, String(v));
  return u.href;
}

/** GET /api/reports/art12?year=&q= → { year, per_sheet, window, workbooks: [{ family, title, device,
 *  file, sheets: [{ name, patients, available, missing }] }] } (annual DRL return, art. 12) */
export function art12Report(params) {
  return isSample ? serverOnly('Reports') : remote('/api/reports/art12', params);
}

/** Download link: the zip of the art. 12 workbooks (needs the pid role). */
export function art12Url({ year, q = 'origin:local' }) {
  if (isSample) return null;
  const u = new URL('/api/reports/art12', MODE);
  for (const [k, v] of Object.entries({ year, q: active(q), format: 'zip' })) u.searchParams.set(k, String(v));
  return u.href;
}

/** Download link for the same report as XLSX (official column order). */
export function inspReportUrl({ year, quarter, q = 'origin:local' }) {
  if (isSample) return null;
  const u = new URL('/api/reports/insp1255', MODE);
  for (const [k, v] of Object.entries({ year, quarter, q: active(q), format: 'xlsx' })) u.searchParams.set(k, String(v));
  return u.href;
}

/** Download link: GET /api/survey/export?month=YYYY-MM&format= (anonymised paediatric survey). */
export function surveyUrl({ month, format = 'csv', q = 'origin:local' }) {
  if (isSample) return null;
  const u = new URL('/api/survey/export', MODE);
  for (const [k, v] of Object.entries({ month, format, q: active(q) })) u.searchParams.set(k, v);
  return u.href;
}

/** GET /api/quality?q=&limit= → { studies, rules: {rule: {severity, title, count}}, devices, flagged } */
export function quality(params = {}) {
  return isSample ? serverOnly('Data quality') : remote('/api/quality', params);
}

/** GET /api/alerts?status=open|acknowledged|all&limit= (same shape as overview.alerts) */
export function alerts(status = 'open', limit = 50) {
  return isSample ? sample('overview.json').then((o) => o.alerts) : remote('/api/alerts', { status, limit });
}
/** POST /api/alerts/{id}/ack {note} (audit-logged) */
export const ackAlert = (id, note) => send('POST', `/api/alerts/${id}/ack`, { note });
/** GET /api/alert-rules · PATCH /api/alert-rules/{id} (admin) */
export function alertRules() {
  return isSample ? serverOnly('Alert rules') : remote('/api/alert-rules');
}
export const updateAlertRule = (id, patch) => send('PATCH', `/api/alert-rules/${id}`, patch);
/** POST /api/alert-rules (admin) · DELETE (only rules not in the seed file) · GET metrics a rule can watch */
export const createAlertRule = (rule) => send('POST', '/api/alert-rules', rule);
export const deleteAlertRule = (id) => send('DELETE', `/api/alert-rules/${id}`);
/** GET /api/settings (admin: site name/code, web address, spool retention, read-only deployment info) · PUT (partial) */
export const siteSettings = () => remote('/api/settings');
export const saveSiteSettings = (body) => send('PUT', '/api/settings', body);
/** GET /api/maintenance (admin: the jobs, the running or last one, the last periodic run) · POST /api/maintenance/{job} {days} starts one */
export const maintenanceInfo = () => remote('/api/maintenance');
export const startMaintenance = (job, days) => send('POST', `/api/maintenance/${job}`, { days: days ?? null });
/** GET /api/notify (admin: fields with their source, channels, status) · PUT (partial; secrets write-only) · POST test */
export const notifySettings = () => remote('/api/notify');
export const saveNotify = (body) => send('PUT', '/api/notify', body);
export const testNotify = (channel) => send('POST', '/api/notify/test', { channel });
export const alertRuleMetrics = () => remote('/api/alert-rules/metrics');

/** GET /api/reports/operators?year=&quarter=&quarters=4 (doses per operator; admin only) */
export function operatorReport(params = {}) {
  return isSample ? serverOnly('Reports') : remote('/api/reports/operators', params);
}
export function operatorReportUrl(params = {}) {
  if (isSample) return null;
  const u = new URL('/api/reports/operators', MODE);
  for (const [k, v] of Object.entries({ ...params, format: 'xlsx' })) if (v != null) u.searchParams.set(k, String(v));
  return u.href;
}

/** GET /api/reports/drl-trend?year=&quarter=&quarters=6&set= (quarterly means vs DRL) */
export function drlTrend(params = {}) {
  return isSample ? serverOnly('Reports') : remote('/api/reports/drl-trend', params);
}
export function drlTrendUrl(params = {}) {
  if (isSample) return null;
  const u = new URL('/api/reports/drl-trend', MODE);
  for (const [k, v] of Object.entries({ ...params, format: 'xlsx' })) if (v != null) u.searchParams.set(k, String(v));
  return u.href;
}

/* ---------------- DICOM nodes (admin) ---------------- */
/** GET /api/dicom/status → { local, receiver, qr, coverage: [{date, done}], nodes } */
export function dicomStatus() {
  return isSample ? serverOnly('DICOM nodes') : remote('/api/dicom/status');
}
export const createNode = (node) => send('POST', '/api/dicom/nodes', node);
export const updateNode = (id, patch) => send('PATCH', `/api/dicom/nodes/${id}`, patch);
export const deleteNode = (id) => send('DELETE', `/api/dicom/nodes/${id}`);
export const echoNode = (id) => send('POST', `/api/dicom/nodes/${id}/echo`);

/** GET /api/backup/status → { state: ok|stale|never, last_ok, age_h, max_hours } (admin, read-only) */
export function backupStatus() {
  return isSample ? serverOnly('Backups') : remote('/api/backup/status');
}

/* ---------------- HIS / RIS servers (admin) ---------------- */
/** GET /api/his/status → { worker, queue: {status: n}, servers } */
export function hisStatus() {
  return isSample ? serverOnly('HIS servers') : remote('/api/his/status');
}
export const createHisServer = (s) => send('POST', '/api/his/servers', s);
export const updateHisServer = (id, patch) => send('PATCH', `/api/his/servers/${id}`, patch);
export const deleteHisServer = (id) => send('DELETE', `/api/his/servers/${id}`);
export const checkHisServer = (id) => send('POST', `/api/his/servers/${id}/check`);
/** POST /api/studies/{id}/his → queue the study for another HIS lookup (admin) */
export const reanalyzeStudy = (id) => send('POST', `/api/studies/${id}/reanalyze`);
export const refetchStudy = (id) => send('POST', `/api/studies/${id}/refetch`);
export const requeryHis = (id) => send('POST', `/api/studies/${id}/his`);

/** GET /api/studies/{id}/dose-statement → { patient, exam, when, device, doses: [{ key, label,
 *  value, unit, note }] } (pid role; audit-logged): the written dose information of art. 4 */
export function doseStatement(id) {
  return isSample ? Promise.reject(new Error('sample mode')) : remote(`/api/studies/${id}/dose-statement`);
}

/** Page that prints it. */
export const doseStatementHref = (id) => `dose-statement.html?id=${encodeURIComponent(id)}`;

/** POST /api/studies/{id}/identify → { name, id, birth_date } (pid role; audit-logged) */
export function identifyStudy(id) {
  return isSample ? Promise.reject(new Error('sample mode')) : send('POST', `/api/studies/${id}/identify`);
}

/* ---------------- users and audit (admin) ---------------- */
export function users() { return isSample ? serverOnly('Users') : remote('/api/users'); }
export const createUser = (u) => send('POST', '/api/users', u);
export const updateUser = (username, patch) => send('PATCH', `/api/users/${encodeURIComponent(username)}`, patch);
export const deleteUser = (username) => send('DELETE', `/api/users/${encodeURIComponent(username)}`);
/** GET /api/audit?limit=&action=&user_name=&text=&since=&before_id= → { rows, actions, more } */
export function auditLog(params = {}) { return isSample ? serverOnly('Audit log') : remote('/api/audit', params); }

/** GET /api/alerts/summary?status=&rule=&level=&since=&until=&modality= → { total, by_rule, by_level, by_modality } */
export function alertSummary(params = {}) {
  return isSample ? serverOnly('Alerts') : remote('/api/alerts/summary', params);
}
/** GET /api/alerts with filters and paging (offset, limit) */
export function alertList(params = {}) {
  return isSample ? sample('overview.json').then((o) => o.alerts) : remote('/api/alerts', params);
}
/** GET /api/alerts/{id}/studies → the patient's studies in the window of a cumulative / count alert */
export const alertStudies = (id) => remote(`/api/alerts/${id}/studies`);
/** POST /api/alerts/ack {ids, note} (audit-logged) */
export const ackAlerts = (ids, note) => send('POST', '/api/alerts/ack', { ids, note });

/** GET /api/studies/{id}/skin → skin dose map estimate { psd_mgy, psd_at, theta, z_cm, dose_mgy, assumptions, ... } */
export function skinDose(id) {
  return isSample ? serverOnly('Skin dose map') : remote(`/api/studies/${id}/skin`);
}

/* ---------------- analysis reports, follow-ups, incidents, QA register, digests ---------------- */
/** GET /api/reports/{name}?year=&quarter=&quarters=&q=… → the report; name is one of ANALYSIS */
export const ANALYSIS = ['dose-check', 'exposure-index', 'collimation', 'scan-length', 'repeats',
  'effective-dose', 'centering', 'optimisation'];
export function analysisReport(name, params = {}) {
  return isSample ? serverOnly('Analysis reports') : remote(`/api/reports/${name}`, params);
}
export function analysisUrl(name, params = {}) {
  if (isSample) return null;
  const u = new URL(`/api/reports/${name}`, MODE);
  for (const [k, v] of Object.entries({ ...params, format: 'csv' })) if (v != null && v !== '') u.searchParams.set(k, String(v));
  return u.href;
}

/** GET /api/followups?status=pending|open|informed|checked|dismissed · POST /api/followups/{id} (admin)
 *  · the patient letter is a page: GET /api/followups/{id}/letter?lang=ro|en (pid role) */
export const followups = (status) => (isSample ? serverOnly('Follow-ups') : remote('/api/followups', { status }));
export const updateFollowup = (id, patch) => send('POST', `/api/followups/${id}`, patch);
export const followupLetterHref = (id, lang = 'ro') => (isSample ? null : new URL(`/api/followups/${id}/letter?lang=${lang}`, MODE).href);

/** GET /api/incidents?status= · GET /api/incidents/{id} · POST /api/incidents {alert_id|study_id, title} (admin)
 *  · POST /api/incidents/{id} {fields…, note, action: close|reopen} */
export const incidents = (status) => (isSample ? serverOnly('Incidents') : remote('/api/incidents', { status }));
export const incident = (id) => remote(`/api/incidents/${id}`);
export const openIncident = (body) => send('POST', '/api/incidents', body);
export const updateIncident = (id, body) => send('POST', `/api/incidents/${id}`, body);

/** GET /api/qa/due?days= · GET/POST /api/devices/{id}/qa · DELETE /api/qa/{id} (admin) */
export const qaDue = (days = 30) => (isSample ? serverOnly('QA register') : remote('/api/qa/due', { days }));
export const deviceQA = (deviceId) => remote(`/api/devices/${deviceId}/qa`);
export const addDeviceQA = (deviceId, body) => send('POST', `/api/devices/${deviceId}/qa`, body);
export const deleteQA = (id) => send('DELETE', `/api/qa/${id}`);

/** GET/PUT /api/digests (admin) · GET /api/digests/{id}/preview */
export const digests = () => (isSample ? serverOnly('Report digests') : remote('/api/digests'));
export const saveDigests = (items) => send('PUT', '/api/digests', items);
export const digestPreview = (id) => remote(`/api/digests/${encodeURIComponent(id)}/preview`);

/** GET/PUT /api/ct-kfactors (PUT admin): the DLP to effective dose coefficients of CT */
export const ctKFactors = () => (isSample ? serverOnly('CT coefficients') : remote('/api/ct-kfactors'));
export const saveCtKFactors = (rows) => send('PUT', '/api/ct-kfactors', rows);
