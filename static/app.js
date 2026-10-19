/*
  app.js
  ------
  All the browser-side code. Plain JavaScript, no framework.

  Core demo version: 5 tabs (Overview, Issues & checks, Return lines,
  Documents, Filing) and 3 roles (preparer, reviewer, partner).

  The pattern used everywhere:
    1. call the server with api('/api/something')
    2. build an HTML string from the JSON
    3. put it into the page with element.innerHTML = html
  Buttons use onclick="someFunction(...)" so it's easy to follow which
  function runs when you click something.
*/

// ---------------------------------------------------------------------------
// Global state (kept tiny on purpose)
// ---------------------------------------------------------------------------
const STATE = {
  boot: null,          // data from /api/bootstrap (users, my permissions...)
  tab: 'overview',     // which tab is open
  selected: new Set(), // line keys ticked for bulk sign-off
  lastBatch: null,     // id of the last bulk action (for Undo)
  issueFilter: 'open',
  lineFilters: { q: '', form: '', only: '' },
};

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

// Which user are we acting as? Remembered in the browser between visits.
function currentUserId() {
  try { return localStorage.getItem('workpaper_user') || 'st-01'; } catch (e) { return 'st-01'; }
}

// Call the server. Every request carries the X-User header (the role switcher).
async function api(path, options = {}) {
  const opts = { ...options, headers: { 'X-User': currentUserId(), ...(options.headers || {}) } };
  if (opts.body) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { data = {}; }
  if (!res.ok) {
    let msg = data.error || ('Request failed: ' + res.status);
    if (data.blockers) msg += '\n- ' + data.blockers.join('\n- ');
    toast(msg, true);
    throw new Error(msg);
  }
  return data;
}

function post(path, body) { return api(path, { method: 'POST', body: body || {} }); }

// Escape text before putting it into HTML (prevents broken pages and XSS).
function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Safe way to pass a string into an onclick="..." attribute.
function arg(s) { return esc(JSON.stringify(s)); }

// Format numbers with commas: 1234567 -> 1,234,567
function fmt(v) {
  if (typeof v === 'number') return v.toLocaleString('en-US', { maximumFractionDigits: 2 });
  if (v === null || v === undefined) return '';
  return esc(v);
}

function money(v) { return typeof v === 'number' ? (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: 0 }) : fmt(v); }

function can(permission) { return STATE.boot && STATE.boot.permissions.includes(permission); }

function toast(message, isError) {
  const t = document.getElementById('toast');
  t.textContent = message;
  t.className = isError ? 'error' : '';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add('hidden'), isError ? 7000 : 3000);
}

function sevBadge(sev) { return `<span class="badge sev-${esc(sev)}">${esc(sev)}</span>`; }

function userName(id) {
  const u = (STATE.boot.users || []).find(x => x.id === id);
  return u ? u.name : (id || '');
}

// A clickable reference. Knows what to open from the shape of the id.
function refLink(ref, text) {
  return `<a onclick="openRef(${arg(ref)})">${esc(text || ref)}</a>`;
}

function openRef(ref) {
  if (/^ev-\d+$/.test(ref)) return openValue(ref);
  if (/^D\d\d$/.test(ref)) return openDoc(ref);
  if (/^(qa|msg|call)-\d+$/.test(ref) || /^(py|agency|ref|asset|household|timeline):/.test(ref)) return openOtherRef(ref);
  return openLine(ref);  // everything else is treated as a return line key
}

function openDrawer(html) {
  document.getElementById('drawer').classList.remove('hidden');
  document.getElementById('drawer-content').innerHTML = html;
  document.getElementById('drawer').scrollTop = 0;
}
function closeDrawer() { document.getElementById('drawer').classList.add('hidden'); }

function signoffBadges(signoffs) {
  const levels = ['preparer', 'reviewer', 'partner'];
  return levels.filter(l => signoffs[l]).map(l => {
    const s = signoffs[l];
    return `<span class="badge ${s.state === 'signed' ? 'ok' : 'reopened'}" title="${esc(s.at)}">${l}: ${s.state === 'signed' ? '✓ ' + esc(userName(s.by)) : 'REOPENED'}</span>`;
  }).join(' ') || '<span class="muted">no sign-offs</span>';
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
const TABS = [
  { id: 'overview', label: 'Overview', render: renderOverview },
  { id: 'issues', label: 'Issues & checks', render: renderIssues },
  { id: 'return', label: 'Return lines', render: renderReturn },
  { id: 'documents', label: 'Documents', render: renderDocuments },
  { id: 'filing', label: 'Filing', render: renderFiling },
];

async function init() {
  STATE.boot = await api('/api/bootstrap');
  document.getElementById('client-name').textContent = STATE.boot.household_name;
  const sel = document.getElementById('user-switcher');
  sel.innerHTML = STATE.boot.users.map(u =>
    `<option value="${esc(u.id)}" ${u.id === STATE.boot.me.id ? 'selected' : ''}>${esc(u.name)} (${esc(u.role)})</option>`).join('');
  document.getElementById('status-badge').textContent = 'Status: ' + STATE.boot.return_status;
  showTab(STATE.tab);
}

function switchUser(id) {
  try { localStorage.setItem('workpaper_user', id); } catch (e) { /* storage blocked: fine */ }
  closeDrawer();
  STATE.selected.clear();
  STATE.lastBatch = null;   // undo only works on your own last action
  init();
}

function renderNav() {
  document.getElementById('nav').innerHTML = TABS.map(t =>
    `<button class="${t.id === STATE.tab ? 'active' : ''}" onclick="showTab('${t.id}')">${t.label}</button>`).join('');
}

async function showTab(id) {
  STATE.tab = id;
  renderNav();
  const tab = TABS.find(t => t.id === id);
  document.getElementById('main').innerHTML = 'Loading...';
  try { await tab.render(); } catch (e) { console.error(e); document.getElementById('main').innerHTML = '<p>Could not load: ' + esc(e.message) + '</p>'; }
}

function refresh() { return showTab(STATE.tab); }

// ---------------------------------------------------------------------------
// OVERVIEW: the bottom line, can we file, the household
// ---------------------------------------------------------------------------
async function renderOverview() {
  const d = await api('/api/summary');
  const e = d.summary.engine, a = d.summary.adjusted;
  const row = (label, jur, key) => {
    const diff = a[jur][key] - e[jur][key];
    return `<tr><td>${label}</td><td class="num">${money(e[jur][key])}</td><td class="num">${money(a[jur][key])}</td>
      <td class="num">${diff ? (diff > 0 ? '+' : '') + money(diff) : ''}</td></tr>`;
  };
  document.getElementById('status-badge').textContent = 'Status: ' + d.status;
  document.getElementById('main').innerHTML = `
    <h2>Overview - 2025 return (${esc(d.household.filing_status.replace(/_/g, ' '))})</h2>
    <div class="grid2">
      <div class="card">
        <h3>Bottom line (engine ${esc(d.summary.version)} vs workpaper)</h3>
        <table><tr><th></th><th>Engine</th><th>Workpaper (est.)</th><th>Change</th></tr>
          ${row('Federal total tax', 'federal', 'total_tax')}
          ${row('Federal payments', 'federal', 'payments')}
          ${row('Federal balance due', 'federal', 'balance_due')}
          ${row('California total tax', 'california', 'total_tax')}
          ${row('California payments', 'california', 'payments')}
          ${row('California balance due (negative = refund)', 'california', 'balance_due')}
        </table>
        <p class="small muted">${esc(d.summary.note)}</p>
        <p>${d.adjustment_count} adjustment(s) recorded.</p>
      </div>
      <div class="card">
        <h3>Can we file? ${d.blockers.length ? '<span class="badge sev-error">No - ' + d.blockers.length + ' blocker(s)</span>' : '<span class="badge ok">Yes</span>'}</h3>
        <ul class="small">${d.blockers.slice(0, 14).map(b => '<li>' + esc(b) + '</li>').join('')}${d.blockers.length > 14 ? '<li>... and ' + (d.blockers.length - 14) + ' more (Filing tab)</li>' : ''}</ul>
        <p>Open issues: ${sevBadge('error')} ${d.issue_counts.error} ${sevBadge('warning')} ${d.issue_counts.warning} ${sevBadge('info')} ${d.issue_counts.info}
          &nbsp; resolved: ${d.issue_counts.resolved} &nbsp; <a onclick="showTab('issues')">open Issues</a></p>
        <p>Whole-return sign-offs: ${signoffBadges(d.return_signoffs)}</p>
      </div>
    </div>
    <div class="card">
      <h3>Household</h3>
      <p>${d.household.people.map(p => esc(p.name) + ' (' + esc(p.role) + ')').join(', ')}</p>
      <p>Residency: ${d.household.residency.map(r => esc(r.state) + ' ' + esc(r.from) + ' to ' + esc(r.to)).join('; ')}</p>
    </div>`;
}

// ---------------------------------------------------------------------------
// ISSUES: our own checks (they find the mistakes) + the engine's diagnostics
// ---------------------------------------------------------------------------
async function renderIssues() {
  const d = await api('/api/issues');
  const f = STATE.issueFilter;
  // "Still to do" = open OR waiting for the partner. Only finished issues go to "resolved".
  const todo = i => i.status === 'open' || i.status === 'waiting_partner';
  const list = d.issues.filter(i => f === 'all' || (f === 'open' ? todo(i) : !todo(i)));
  const total = list.reduce((s, i) => ({ fed: s.fed + (i.status === 'open' ? i.effect.federal : 0), ca: s.ca + (i.status === 'open' ? i.effect.california : 0) }), { fed: 0, ca: 0 });
  document.getElementById('main').innerHTML = `
    <h2>Issues & checks</h2>
    <p>Our own checks go back to the printed page, the client's answers and the agency records, and do not trust the engine.
      The engine's own diagnostics are listed too (source: engine).</p>
    <div class="row">Show:
      ${['open', 'resolved', 'all'].map(x => `<button class="${f === x ? 'primary' : ''}" onclick="STATE.issueFilter='${x}';renderIssues()">${x}</button>`).join('')}
      <span>Estimated effect of open issues on balance due: federal <b>${money(total.fed)}</b>, California <b>${money(total.ca)}</b></span>
      ${STATE.lastBatch ? `<button onclick="undoLast()">Undo last action</button>` : ''}
    </div>
    ${list.map(renderIssueCard).join('') || '<p>Nothing here.</p>'}`;
}

function renderIssueCard(i) {
  const evidence = i.evidence.map(r => refLink(r)).join(', ');
  const lines = i.lines.map(k => refLink(k)).join(', ');
  const sugg = i.suggestions.length ? `
    <table class="small"><tr><th>Proposed adjustment</th><th>From</th><th>To</th><th>Federal</th><th>California</th><th>Reason</th></tr>
      ${i.suggestions.map(s => `<tr><td>${refLink(s.target_id)}</td><td class="num">${fmt(s.old_value)}</td><td class="num">${fmt(s.new_value)}</td>
        <td class="num">${money(s.federal_effect)}</td><td class="num">${money(s.california_effect)}</td><td>${esc(s.reason)}</td></tr>`).join('')}
    </table>` : '';
  const id = arg(i.id);
  // Waiting for partner: say so clearly, and give the partner the Approve button right here.
  const waitingBox = i.status === 'waiting_partner' ? `
    <span class="badge sev-warning">waiting for partner approval</span>
    <span class="small">Tax effect is over 1,000, so a partner must approve before this counts.</span>
    ${can('approve_adjustment')
      ? i.pending_adjustments.map(a => `<button class="primary" onclick="approveAdj(${a})">Approve adjustment #${a}</button>`).join('')
      : '<span class="small muted">(Only a partner can approve.)</span>'}` : '';
  const actions = i.status === 'waiting_partner' ? waitingBox : i.status === 'open' ? `
    ${i.suggestions.length && can('adjust') ? `<button class="primary" onclick="applyIssue(${id})">Apply proposed adjustment(s)</button>` : ''}
    ${can('update_issue') ? `<input id="issue-note-${esc(i.id)}" placeholder="Reason (required to accept/dismiss)" size="40">
      <button onclick="setIssueStatus(${id}, 'accepted')">Accept as is</button>
      <button onclick="setIssueStatus(${id}, 'dismissed')">Dismiss</button>` : ''}`
    : `<span class="badge ok">${esc(i.status)}</span> ${esc(i.status_note || '')} ${i.status_by ? '- ' + esc(userName(i.status_by)) : ''}
       ${can('update_issue') && i.status !== 'adjusted' ? `<button onclick="setIssueStatus(${id}, 'open')">Reopen</button>` : ''}`;
  return `<div class="card">
    <div class="row">${sevBadge(i.severity)} <span class="badge">${esc(i.source)}</span> <span class="badge">${esc(i.area)}</span>
      <b>${esc(i.title)}</b></div>
    <p>${esc(i.explanation)}</p>
    <p class="small">Evidence: ${evidence || '-'} &nbsp; | &nbsp; Lines: ${lines || '-'}
      &nbsp; | &nbsp; Est. effect on balance due: federal ${money(i.effect.federal)}, California ${money(i.effect.california)}</p>
    ${sugg}
    <div class="row">${actions}</div>
  </div>`;
}

async function applyIssue(issueId) {
  const r = await post('/api/issues/' + encodeURIComponent(issueId) + '/apply');
  STATE.lastBatch = r.batch_id;
  toast('Adjustment(s) recorded. Effects over 1,000 wait for partner approval.');
  renderIssues();
}

async function setIssueStatus(issueId, status) {
  const box = document.getElementById('issue-note-' + issueId);
  await post('/api/issues/' + encodeURIComponent(issueId) + '/status', { status, note: box ? box.value : '' });
  toast('Issue ' + status);
  renderIssues();
}

async function undoLast() {
  if (!STATE.lastBatch) return;
  await post('/api/undo', { batch_id: STATE.lastBatch });
  STATE.lastBatch = null;
  toast('Undone');
  refresh();
}

// ---------------------------------------------------------------------------
// RETURN LINES (with bulk sign-off and undo)
// ---------------------------------------------------------------------------
async function renderReturn() {
  const f = STATE.lineFilters;
  const params = new URLSearchParams({ q: f.q, form: f.form });
  if (f.only) params.set('only', f.only);
  const d = await api('/api/lines?' + params.toString());
  document.getElementById('main').innerHTML = `
    <h2>Return lines</h2>
    <div class="row">
      Form <select onchange="STATE.lineFilters.form=this.value;renderReturn()"><option value="">all forms</option>
        ${STATE.boot.forms.map(x => `<option ${x === f.form ? 'selected' : ''}>${x}</option>`).join('')}</select>
      Show <select onchange="STATE.lineFilters.only=this.value;renderReturn()">
        ${[['', 'all'], ['flagged', 'flagged by checks'], ['adjusted', 'adjusted']].map(([v, l]) => `<option value="${v}" ${v === f.only ? 'selected' : ''}>${l}</option>`).join('')}</select>
      <input placeholder="filter key or label" value="${esc(f.q)}" onkeydown="if(event.key==='Enter'){STATE.lineFilters.q=this.value;renderReturn()}">
      <span class="muted">${d.lines.length} lines (pick form F8949 to see each stock sale)</span>
    </div>
    ${can('bulk') ? `<div class="row card">Bulk on <b id="sel-count">${STATE.selected.size}</b> selected:
      ${['preparer', 'reviewer', 'partner'].filter(l => can('signoff_' + l)).map(l => `<button onclick="bulkSignoff('${l}')">Sign off as ${l}</button>`).join('')}
      ${STATE.lastBatch ? '<button onclick="undoLast()">Undo last action</button>' : ''}
      <button onclick="STATE.selected.clear();renderReturn()">Clear selection</button></div>` : ''}
    <table>
      <tr><th><input type="checkbox" onclick="selectAllLines(this.checked)"></th><th>Line</th><th>Label</th>
        <th>Engine</th><th>Workpaper</th><th>Status</th></tr>
      ${d.lines.map(l => `<tr>
        <td><input type="checkbox" class="line-check" data-key="${esc(l.key)}" ${STATE.selected.has(l.key) ? 'checked' : ''} onchange="toggleSelect(${arg(l.key)}, this.checked)"></td>
        <td>${refLink(l.key)}</td><td>${esc(l.label)}</td>
        <td class="num">${fmt(l.engine_value)}</td>
        <td class="num">${l.adjusted ? '<b>' + fmt(l.value) + '</b>' : fmt(l.value)}</td>
        <td>${l.flags.map(s => sevBadge(s)).join('')}
            ${l.adjusted ? '<span class="badge">' + (l.pending ? 'pending partner' : 'adjusted') + '</span>' : ''}</td></tr>`).join('')}
    </table>`;
}

function toggleSelect(key, on) {
  if (on) STATE.selected.add(key); else STATE.selected.delete(key);
  const c = document.getElementById('sel-count'); if (c) c.textContent = STATE.selected.size;
}
function selectAllLines(on) {
  document.querySelectorAll('.line-check').forEach(cb => { cb.checked = on; toggleSelect(cb.dataset.key, on); });
}
async function bulkSignoff(level) {
  if (!STATE.selected.size) return toast('Select some lines first', true);
  const r = await post('/api/signoffs', { level, targets: [...STATE.selected].map(id => ({ type: 'line', id })) });
  STATE.lastBatch = r.batch_id;
  toast('Signed off' + (r.refused.length ? ' (refused on ' + r.refused.length + ' you prepared yourself)' : ''));
  renderReturn();
}

// ---------------------------------------------------------------------------
// LINE DETAIL (drawer): how the number was built, where it came from, who signed it
// ---------------------------------------------------------------------------
async function openLine(key) {
  let d;
  try { d = await api('/api/lines/' + encodeURIComponent(key)); } catch (e) { return; }
  const l = d.line;
  const adjusted = d.effective_value !== l.value;
  const cats = STATE.boot.tax_effect_categories;
  openDrawer(`
    <h2>${esc(l.key)} <span class="muted">${esc(l.label)}</span></h2>
    <p style="font-size:20px">Workpaper value: <b>${fmt(d.effective_value)}</b>
      ${adjusted ? '<span class="badge">adjusted - engine said ' + fmt(l.value) + '</span>' : ''}</p>
    <p>Area: ${esc(d.area)} | Jurisdiction: ${esc(l.jurisdiction)} | Sign-offs: ${signoffBadges(d.signoffs)}</p>
    ${d.issues.length ? '<div class="card">' + d.issues.map(i => sevBadge(i.severity) + ' ' + esc(i.title) + ' <span class="muted">(' + esc(i.status) + ')</span>').join('<br>') + '</div>' : ''}

    <h3>How it was built</h3>
    <p>Rule <b>${esc(l.rule)}</b>: ${esc(d.engine_rule || '')}</p>
    ${l.note ? '<p>Engine note: <i>' + esc(l.note) + '</i></p>' : ''}
    ${d.inputs.length ? '<p>Inputs:</p><ul>' + d.inputs.map(i => `<li>${refLink(i.key)} ${esc(i.label)} = <b>${fmt(i.value)}</b></li>`).join('') + '</ul>' : ''}
    ${d.used_by.length ? '<p>Used by (downstream, NOT recomputed when we adjust - the engine cannot be re-run):</p><ul>' + d.used_by.map(i => `<li>${refLink(i.key)} ${esc(i.label)}</li>`).join('') + '</ul>' : ''}

    <h3>Where it came from (evidence)</h3>
    ${d.sources.length ? '<ul>' + d.sources.map(s => `<li>${refLink(s.ref)} - ${esc(s.label)}
        ${s.value_detail ? ' <span class="badge ' + (s.value_detail.confidence < 0.6 ? 'sev-error' : '') + '">confidence ' + Math.round(s.value_detail.confidence * 100) + '%</span>' : ''}</li>`).join('') + '</ul>'
      : '<p class="muted">No direct sources: computed from inputs.</p>'}

    <h3>Adjustments (original is never lost)</h3>
    ${d.adjustments.length ? `<table class="small"><tr><th>#</th><th>From</th><th>To</th><th>Why</th><th>Who</th><th>Fed/CA effect</th><th>Status</th><th></th></tr>
      ${d.adjustments.map(a => `<tr><td>${a.id}</td><td>${fmt(a.old_value)}</td><td>${fmt(a.new_value)}</td><td>${esc(a.reason)}</td>
        <td>${esc(a.created_by_name)}<br>${esc(a.created_at)}</td><td>${money(a.federal_effect)} / ${money(a.california_effect)}</td>
        <td>${esc(a.status)}${a.approved_by ? ' by ' + esc(userName(a.approved_by)) : ''}</td>
        <td>${a.status !== 'reverted' && can('adjust') ? `<button onclick="revertAdj(${a.id}, ${arg(l.key)})">Revert</button>` : ''}
            ${a.status === 'pending_partner' && can('approve_adjustment') ? `<button class="primary" onclick="approveAdj(${a.id}, ${arg(l.key)})">Approve</button>` : ''}</td></tr>`).join('')}</table>`
      : '<p class="muted">None.</p>'}
    ${can('adjust') ? `<div class="card">
      New value <input id="adj-value" value="${esc(d.effective_value)}" size="12">
      Tax-effect category <select id="adj-cat">${cats.map(c => `<option ${c === d.suggested_category ? 'selected' : ''}>${c}</option>`).join('')}</select><br>
      Reason <input id="adj-reason" size="60" placeholder="why (required)">
      <button class="primary" onclick="saveAdjustment(${arg(l.key)})">Save adjustment</button></div>` : ''}

    <h3>Sign-off</h3>
    <div class="row">${['preparer', 'reviewer', 'partner'].filter(x => can('signoff_' + x)).map(x => `<button onclick="signOne('line', ${arg(l.key)}, '${x}')">Sign off as ${x}</button>`).join('')}
      <span class="small muted">A sign-off reopens automatically if this value changes.</span></div>
  `);
}

async function saveAdjustment(key) {
  const body = { target_type: 'line', target_id: key, new_value: document.getElementById('adj-value').value,
                 category: document.getElementById('adj-cat').value, reason: document.getElementById('adj-reason').value };
  const r = await post('/api/adjustments', body);
  toast('Adjustment #' + r.id + ' saved');
  openLine(key);
}
async function revertAdj(id, key) { await post('/api/adjustments/' + id + '/revert'); toast('Reverted'); openLine(key); }
async function approveAdj(id, key) { await post('/api/adjustments/' + id + '/approve'); toast('Approved'); if (key) openLine(key); else refresh(); }

async function signOne(type, id, level) {
  const r = await post('/api/signoffs', { level, targets: [{ type, id }] });
  if (r.refused.length) toast('Refused: you cannot review work you prepared yourself.', true); else toast('Signed off as ' + level);
  if (type === 'line') openLine(id); else refresh();
}

// ---------------------------------------------------------------------------
// DOCUMENTS: simulated source documents with the extracted fields drawn on them
// ---------------------------------------------------------------------------
async function renderDocuments() {
  const d = await api('/api/documents');
  const counts = {};
  d.documents.forEach(x => counts[x.state] = (counts[x.state] || 0) + 1);
  document.getElementById('main').innerHTML = `
    <h2>Documents (${d.documents.length})</h2>
    <p>Every document should be <b>used</b>, <b>superseded</b> or <b>reference-only</b>.
      ${Object.entries(counts).map(([k, v]) => '<span class="badge">' + esc(k) + ': ' + v + '</span>').join(' ')}</p>
    <table><tr><th>Doc</th><th>Title</th><th>Received</th><th>State</th><th>Detail</th></tr>
      ${d.documents.map(x => `<tr><td>${refLink(x.id)}</td><td>${esc(x.title)}</td><td>${esc(x.received_at.slice(0, 10))}</td>
        <td><span class="badge ${x.state === 'used' ? 'ok' : (x.state === 'unused' || x.state === 'used_no_line') ? 'sev-warning' : ''}">${esc(x.state)}</span></td>
        <td class="small">${esc(x.detail)}</td></tr>`).join('')}
    </table>`;
}

const DOC_VIEW = { doc: null, page: 1, focus: null };

async function openDoc(docId, focusEv) {
  const d = await api('/api/documents/' + docId);
  DOC_VIEW.doc = d;
  DOC_VIEW.focus = focusEv || null;
  DOC_VIEW.page = 1;
  if (focusEv) {
    const pg = d.pages.find(p => p.fields.some(f => f.id === focusEv));
    if (pg) DOC_VIEW.page = pg.page;
  }
  drawDoc();
}

function drawDoc() {
  const d = DOC_VIEW.doc, m = d.document;
  const page = d.pages.find(p => p.page === DOC_VIEW.page) || d.pages[0];
  const focus = page.fields.find(f => f.id === DOC_VIEW.focus);
  // Highlight the printed text of every extracted value; the focused one in orange.
  // We do it in ONE pass with one regular expression. (Replacing values one by
  // one would also replace text inside the <mark> tags we already inserted.)
  let text = esc(page.render.text);
  const byNeedle = {};
  page.fields.forEach(f => {
    if (!f.text_as_read || f.sensitive || String(f.text_as_read).length < 3) return;
    const needle = esc(f.text_as_read);
    if (!byNeedle[needle] || f.id === DOC_VIEW.focus) byNeedle[needle] = f;
  });
  const needles = Object.keys(byNeedle).sort((a, b) => b.length - a.length);  // longest first
  if (needles.length) {
    const pattern = new RegExp(needles.map(n => n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g');
    text = text.replace(pattern, m => {
      const f = byNeedle[m];
      return `<mark class="${f.id === DOC_VIEW.focus ? 'selected' : ''}" title="${esc(f.id)} ${esc(f.label)}">${m}</mark>`;
    });
  }
  openDrawer(`
    <h2>${esc(m.id)} ${esc(m.title)}</h2>
    <p class="small">Issuer: ${esc(m.issuer)} | belongs to: ${esc(m.belongs_to)} | received ${esc(m.received_at)} by ${esc(m.uploaded_by)}
      | file ${esc(m.file_name)} ${m.marked_corrected ? '| <b>CORRECTED</b>' : ''}<br>
      Extraction: ${esc(m.extraction.status)} ${esc(m.extraction.reason || '')}<br>
      Used in engine versions: ${esc(d.in_versions.join(', ') || 'none')} | State: <b>${esc(d.accounting ? d.accounting.state : '')}</b>
      ${d.lines.length ? ' | Feeds: ' + d.lines.map(k => refLink(k)).join(', ') : ''}</p>
    <div class="row">Page ${page.page} of ${d.pages.length}
      <button ${page.page <= 1 ? 'disabled' : ''} onclick="DOC_VIEW.page=${page.page - 1};drawDoc()">◀ prev</button>
      <button ${page.page >= d.pages.length ? 'disabled' : ''} onclick="DOC_VIEW.page=${page.page + 1};drawDoc()">next ▶</button>
      <span class="small muted">Simulated page (no real PDF in the packet). Boxes = fields the AI extracted, at their recorded positions
      (hover for details; on busy pages labels show only for the selected box). Red = low confidence; dashed = not used by any return line.</span></div>
    <div class="paper">
      ${page.fields.map(f => {
        const p = f.position || { x0: 0, y0: 0, x1: 0.2, y1: 0.03 };
        const cls = (f.confidence < 0.6 ? ' lowconf' : '') + (f.used_by.length ? '' : ' unused') + (f.id === DOC_VIEW.focus ? ' selected' : '');
        return `<div class="field-box${cls}" style="left:${p.x0 * 100}%;top:${p.y0 * 100}%;width:${(p.x1 - p.x0) * 100}%;height:${Math.max((p.y1 - p.y0) * 100, 3)}%"
          title="${esc(f.id)} ${esc(f.label)} = ${esc(f.text_as_read)} (conf ${f.confidence})" onclick="DOC_VIEW.focus=${arg(f.id)};drawDoc()">${f.id === DOC_VIEW.focus || page.fields.length < 25 ? esc(f.field) + ': ' + esc(f.text_as_read) : ''}</div>`;
      }).join('')}
    </div>
    ${focus ? `<div class="card"><b>${esc(focus.id)}</b> field <b>${esc(focus.field)}</b> "${esc(focus.label)}" = <b>${esc(focus.text_as_read)}</b>
      (value ${esc(focus.value)}, type ${esc(focus.value_type)}, confidence ${Math.round(focus.confidence * 100)}%)<br>
      Landed in: ${focus.used_by.length ? focus.used_by.map(k => refLink(k)).join(', ') : '<b>no return line</b>'}</div>`
      : '<p class="small muted">Click a box to see where that value landed.</p>'}
    <h3>Printed content (the truth)</h3>
    <div class="printed">${text}</div>
    <h3>All fields on this page (${page.fields.length})</h3>
    <table class="small"><tr><th>Value</th><th>Field</th><th>Read as</th><th>Conf.</th><th>Landed in</th></tr>
      ${page.fields.map(f => `<tr><td><a onclick="DOC_VIEW.focus=${arg(f.id)};drawDoc()">${esc(f.id)}</a></td><td>${esc(f.label)}</td>
        <td>${esc(f.text_as_read)}</td><td>${Math.round(f.confidence * 100)}%</td>
        <td>${f.used_by.map(k => refLink(k)).join(', ') || '<span class="muted">-</span>'}</td></tr>`).join('')}</table>
  `);
}

async function openValue(evId) {
  const d = await api('/api/values/' + evId);
  openDoc(d.value.document_id, evId);
}

// Other evidence: the client's questionnaire answers, messages, IRS / FTB records
async function openOtherRef(ref) {
  const res = await fetch('/api/client-ref/' + encodeURIComponent(ref), { headers: { 'X-User': currentUserId() } });
  const text = await res.text();
  openDrawer(`<h2>${esc(ref)}</h2><pre>${esc(text)}</pre>`);
}

// ---------------------------------------------------------------------------
// FILING: approvals, sign the whole return, blockers, e-file
// ---------------------------------------------------------------------------
async function renderFiling() {
  const [f, s, adj] = await Promise.all([api('/api/filing'), api('/api/summary'), api('/api/adjustments')]);
  const pending = adj.adjustments.filter(a => a.status === 'pending_partner');
  const filed = f.status === 'filed';
  document.getElementById('main').innerHTML = `
    <h2>Filing</h2>
    <p>Status: <span class="badge ${filed ? 'ok' : ''}">${esc(f.status)}</span>
      ${filed ? ' The return is frozen: no more changes.' : ''}</p>
    ${pending.length ? `<div class="card"><h3>Adjustments waiting for partner approval (firm rule: effect over 1,000)</h3>
      <table class="small"><tr><th>#</th><th>Line</th><th>From → To</th><th>Effect fed / CA</th><th>Why</th><th>By</th><th></th></tr>
      ${pending.map(a => `<tr><td>${a.id}</td><td>${refLink(a.target_id)}</td><td>${fmt(a.old_value)} → ${fmt(a.new_value)}</td>
        <td>${money(a.federal_effect)} / ${money(a.california_effect)}</td><td>${esc(a.reason)}</td><td>${esc(a.created_by_name)}</td>
        <td>${can('approve_adjustment') ? `<button class="primary" onclick="approveAdj(${a.id})">Approve</button>` : ''}</td></tr>`).join('')}</table>
      ${can('approve_adjustment') ? `<button onclick="approveAll(${arg(pending.map(a => a.id))})">Approve all</button>` : '<p class="small muted">Only a partner can approve.</p>'}</div>` : ''}
    <div class="card"><h3>1. Whole-return sign-off</h3>
      <p>${signoffBadges(s.return_signoffs)}</p>
      ${filed ? '' : ['preparer', 'reviewer', 'partner'].filter(l => can('signoff_' + l)).map(l => `<button onclick="signOne('return', '2025', '${l}')">Sign the whole return as ${l}</button>`).join('')}
      <p class="small muted">If any number changes after signing, the sign-off shows REOPENED and blocks filing.</p></div>
    <div class="card"><h3>2. Blockers (${f.blockers.length})</h3>
      <ul>${f.blockers.map(b => '<li>' + esc(b) + '</li>').join('') || '<li>None - ready to file.</li>'}</ul></div>
    <div class="card"><h3>3. File</h3>
      ${filed ? '<p>Filed and accepted.</p>' : can('file_return') ? `<button class="primary" onclick="efile()">E-file return (federal + California)</button>` : '<p class="muted">Only a partner can file.</p>'}
      <p class="small muted">E-file is simulated. It validates dependents' SSNs like the IRS does and reproduces the packet's rejection (R0000-504-02) if Theo's SSN is still wrong.</p></div>
    <div class="card"><h3>Filing history</h3>
      <table class="small"><tr><th>#</th><th>Jurisdiction</th><th>Status</th><th>Detail</th><th>By / when</th></tr>
        ${f.filings.map(x => `<tr><td>${x.id}</td><td>${esc(x.jurisdiction)}</td>
          <td><span class="badge ${x.status === 'accepted' ? 'ok' : x.status === 'rejected' ? 'sev-error' : ''}">${esc(x.status)}</span></td>
          <td>${esc(x.detail)}</td><td>${esc(x.created_by_name)}<br>${esc(x.created_at)}</td></tr>`).join('') || '<tr><td colspan="5">Nothing filed yet.</td></tr>'}</table></div>`;
}

async function approveAll(ids) { for (const id of ids) await post('/api/adjustments/' + id + '/approve'); toast('Approved ' + ids.length); renderFiling(); }
async function efile() {
  const r = await post('/api/filing/efile', {});
  toast(r.status === 'accepted' ? 'Accepted! Return is now frozen.' : 'Rejected: ' + r.detail, r.status !== 'accepted');
  init();
}

// ---------------------------------------------------------------------------
// SEARCH (top bar): lines, documents, extracted values
// ---------------------------------------------------------------------------
async function runSearch() {
  const q = document.getElementById('search-box').value;
  const d = await api('/api/search?q=' + encodeURIComponent(q));
  openDrawer(`<h2>Search: ${esc(q)}</h2><p>${d.results.length} result(s)</p>
    <table class="small">${d.results.map(r => `<tr><td>${esc(r.kind)}</td><td>${refLink(String(r.id), r.text)}</td></tr>`).join('')}</table>`);
}

// Start!
init();
