// "Re-analyze" and "Re-fetch" buttons of a study page (admin; the server enforces the role).
import { h, toast } from '../core/dom.js';
import { confirmDialog } from '../core/confirm.js';
import { reanalyzeStudy, refetchStudy, isSample } from '../core/api.js';

export function studyActions(s) {
  if (isSample) return null;
  const analyze = h('button', { class: 'btn no-print', type: 'button',
    title: 'Classify and derive this study again from its stored data' }, 'Re-analyze');
  const fetch = h('button', { class: 'btn no-print', type: 'button',
    title: 'Delete this study and retrieve it from the PACS again' }, 'Re-fetch');
  analyze.addEventListener('click', async () => {
    analyze.disabled = true;
    try { await reanalyzeStudy(s.id); toast('Re-analyzed'); setTimeout(() => location.reload(), 600); } catch (e) { toast(e.message); analyze.disabled = false; }
  });
  fetch.addEventListener('click', async () => {
    if (!(await confirmDialog('Delete this study and retrieve it from the PACS again? It is missing until the retrieval finishes.', { title: 'Re-fetch study', label: 'Re-fetch' }))) return;
    fetch.disabled = true;
    try { await refetchStudy(s.id); toast('Re-fetch started: the study returns when it is imported again'); } catch (e) { toast(e.message); fetch.disabled = false; }
  });
  return h('span', { class: 'no-print', style: { display: 'inline-flex', gap: '6px' } }, analyze, fetch);
}
