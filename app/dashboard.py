"""The interactive control-panel dashboard: populate data, see the

transaction list as a business owner would, launch recovery actions, and
watch the (simulated) customer responses come back with real KPIs
updating live. Plain server-rendered HTML + vanilla JS (fetch, no build
step, no framework) -- matches the plan's own non-goal of a React SPA
while still being a real, driven, stateful interface rather than a
collection of static links.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app import orchestrator

router = APIRouter()


class BatchExplainRequest(BaseModel):
    batch_id: str


class TransactionExplainRequest(BaseModel):
    at_risk_id: str


@router.post("/dashboard/api/populate")
def api_populate():
    return JSONResponse(orchestrator.populate())


@router.get("/dashboard/api/kpis")
def api_kpis():
    return JSONResponse(orchestrator.get_kpis())


@router.get("/dashboard/api/transactions")
def api_transactions():
    return JSONResponse(orchestrator.get_transactions())


@router.post("/dashboard/api/launch-recovery")
def api_launch_recovery():
    return JSONResponse(orchestrator.launch_recovery())


@router.post("/dashboard/api/simulate-replies")
def api_simulate_replies():
    return JSONResponse(orchestrator.simulate_replies())


@router.post("/dashboard/api/explain-batch")
def api_explain_batch(body: BatchExplainRequest):
    return JSONResponse(orchestrator.explain_batch(body.batch_id))


@router.post("/dashboard/api/explain-batch-groups")
def api_explain_batch_groups(body: BatchExplainRequest):
    return JSONResponse(orchestrator.explain_batch_groups(body.batch_id))


@router.post("/dashboard/api/explain-transaction")
def api_explain_transaction(body: TransactionExplainRequest):
    return JSONResponse(orchestrator.explain_transaction(body.at_risk_id))


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    return HTMLResponse(_PAGE)


_PAGE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Revenue Recovery</title>
<style>
  :root {
    --bg: #f4f5f0; --bg-raised: #ffffff; --bg-sunken: #eceee7;
    --ink: #1b1f1c; --ink-muted: #565f57; --ink-faint: #8b9389;
    --rule: #d9dcd2; --rule-soft: #e7e9e1;
    --accent: #1f5d50; --accent-strong: #163f37; --accent-soft: #e2ede8;
    --amber: #96661a; --amber-soft: #f2e6cf;
    --blue: #4f5a86; --blue-soft: #e6e7f2;
    --red: #a13c30; --red-soft: #f4e2df;
    --shadow: 0 1px 2px rgba(27,31,28,.04), 0 8px 24px -12px rgba(27,31,28,.12);
    --font: -apple-system, 'Segoe UI', system-ui, sans-serif;
    --mono: 'SF Mono', 'Consolas', 'Cascadia Mono', monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink); font-family: var(--font); font-size: 14.5px; line-height: 1.5; }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 2em 2em 5em; }

  header.top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.6em; flex-wrap: wrap; gap: .8em; }
  header.top h1 { font-size: 1.5rem; margin: 0; font-weight: 700; letter-spacing: -.01em; }
  header.top .sub { color: var(--ink-muted); font-size: .88rem; margin-top: .15em; }
  .business-badge { font-family: var(--mono); font-size: .74rem; color: var(--ink-faint); background: var(--bg-sunken); padding: .3em .7em; border-radius: 6px; }

  .actions { display: flex; gap: .9em; flex-wrap: wrap; margin-bottom: 1.6em; }
  button.primary, button.secondary {
    font-family: var(--font); font-size: .92rem; font-weight: 600; padding: .75em 1.3em; border-radius: 8px;
    border: 1px solid transparent; cursor: pointer; display: inline-flex; align-items: center; gap: .55em;
    transition: opacity .15s ease, transform .1s ease;
  }
  button.primary { background: var(--accent); color: #f4faf7; }
  button.primary:hover:not(:disabled) { background: var(--accent-strong); }
  button.secondary { background: var(--bg-raised); color: var(--ink); border-color: var(--rule); }
  button.secondary:hover:not(:disabled) { background: var(--bg-sunken); }
  button:disabled { opacity: .55; cursor: not-allowed; }
  button:active:not(:disabled) { transform: scale(.98); }
  .spinner { width: 14px; height: 14px; border-radius: 50%; border: 2px solid rgba(255,255,255,.4); border-top-color: #fff; animation: spin .7s linear infinite; display: none; }
  button.secondary .spinner { border-color: rgba(27,31,28,.25); border-top-color: var(--ink); }
  button.loading .spinner { display: inline-block; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .status-line { display: flex; align-items: center; gap: .6em; font-size: .86rem; color: var(--ink-muted); min-height: 1.4em; margin: -.6em 0 1.4em; }
  .status-line .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ink-faint); flex: none; }
  .status-line.ok .dot { background: var(--accent); }
  .status-line.busy .dot { background: var(--amber); animation: pulse 1s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .3; } }

  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(165px, 1fr)); gap: .9em; margin-bottom: 1.8em; }
  .kpi { background: var(--bg-raised); border: 1px solid var(--rule); border-radius: 10px; padding: 1em 1.2em; box-shadow: var(--shadow); }
  .kpi .label { font-size: .72rem; text-transform: uppercase; letter-spacing: .05em; color: var(--ink-faint); font-weight: 600; }
  .kpi .value { font-family: var(--mono); font-size: 1.55rem; font-weight: 600; margin-top: .3em; font-variant-numeric: tabular-nums; }
  .kpi .value.accent { color: var(--accent-strong); }
  .kpi .value.up { color: var(--accent-strong); }
  .kpi .value.down { color: var(--red); }
  .kpi .foot { font-size: .78rem; color: var(--ink-muted); margin-top: .25em; }

  section.panel { background: var(--bg-raised); border: 1px solid var(--rule); border-radius: 12px; box-shadow: var(--shadow); margin-bottom: 1.6em; overflow: hidden; }
  section.panel > .panel-head { padding: 1em 1.3em; border-bottom: 1px solid var(--rule); display: flex; align-items: center; justify-content: space-between; }
  section.panel > .panel-head h2 { font-size: 1.02rem; margin: 0; font-weight: 700; }
  section.panel > .panel-head .count { font-family: var(--mono); font-size: .8rem; color: var(--ink-faint); }

  table { border-collapse: collapse; width: 100%; font-size: .86rem; }
  thead th { text-align: left; font-family: var(--mono); font-size: .68rem; letter-spacing: .05em; text-transform: uppercase; color: var(--ink-faint); padding: .7em 1em; border-bottom: 1px solid var(--rule); white-space: nowrap; }
  tbody td { padding: .62em 1em; border-bottom: 1px solid var(--rule-soft); white-space: nowrap; }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: var(--bg-sunken); }
  td.num { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }
  td.mono { font-family: var(--mono); font-size: .82em; color: var(--ink-muted); }
  .table-scroll { overflow-x: auto; max-height: 480px; overflow-y: auto; }

  .pill { display: inline-flex; align-items: center; padding: .18em .6em; border-radius: 5px; font-size: .74rem; font-weight: 600; font-family: var(--mono); }
  .pill.CAPTURED { background: var(--accent-soft); color: var(--accent-strong); }
  .pill.FAILED { background: var(--red-soft); color: var(--red); }
  .pill.CREATED, .pill.AUTHORIZED { background: var(--amber-soft); color: var(--amber); }
  .pill.REFUNDED { background: var(--blue-soft); color: var(--blue); }

  .empty-state { padding: 3em 1.5em; text-align: center; color: var(--ink-muted); }
  .empty-state .big { font-size: 2rem; margin-bottom: .3em; }

  .log { font-family: var(--mono); font-size: .78rem; background: #14171a; color: #d6dad3; border-radius: 10px; padding: 1em 1.2em; max-height: 220px; overflow-y: auto; box-shadow: var(--shadow); }
  .log .line { padding: .15em 0; opacity: 0; animation: fadein .25s ease forwards; }
  .log .line .t { color: #6b736a; margin-right: .6em; }
  .log .line.ok { color: #7ecab6; }
  .log .line.warn { color: #d9ab5c; }
  @keyframes fadein { to { opacity: 1; } }

  .badge { display: inline-flex; align-items: center; gap: .35em; padding: .28em .7em; border-radius: 20px; font-size: .7rem; font-weight: 700; text-transform: uppercase; letter-spacing: .03em; }
  .badge-ai { background: var(--accent-soft); color: var(--accent-strong); }
  .badge-fallback { background: var(--amber-soft); color: var(--amber); }
  .explain-body { padding: 1.1em 1.3em; }
  .narrative-text { font-size: .95rem; line-height: 1.65; color: var(--ink); }
  .narrative-note { font-size: .78rem; color: var(--ink-faint); margin-top: .6em; }
  .explain-btn { font-family: var(--mono); font-size: .72rem; font-weight: 600; background: var(--bg-raised); border: 1px solid var(--rule); border-radius: 5px; padding: .3em .65em; cursor: pointer; color: var(--ink-muted); }
  .explain-btn:hover:not(:disabled) { background: var(--bg-sunken); color: var(--ink); }
  .explain-btn:disabled { opacity: .6; cursor: default; }
  .row-narrative { font-size: .84rem; color: var(--ink-muted); white-space: normal; line-height: 1.6; padding: .3em 0; }
  .row-narrative .badge { margin-bottom: .4em; }
  tr.explain-detail-row td { background: var(--bg-sunken); white-space: normal; }

  .group-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1em; }
  .group-card { border: 1px solid var(--rule); border-radius: 10px; padding: 1.1em 1.3em; background: var(--bg-raised); }
  .group-card .group-head { display: flex; align-items: flex-start; justify-content: space-between; gap: .6em; margin-bottom: .35em; }
  .group-card .group-title { font-weight: 700; font-size: .95rem; }
  .group-card .group-meta { font-family: var(--mono); font-size: .76rem; color: var(--ink-faint); margin-bottom: .65em; }
  .group-card .group-narrative { font-size: .85rem; line-height: 1.55; color: var(--ink); }
  .group-card .group-toggle { margin-top: .8em; font-family: var(--mono); font-size: .72rem; font-weight: 600; background: none; border: 1px solid var(--rule); border-radius: 5px; padding: .3em .65em; cursor: pointer; color: var(--ink-muted); }
  .group-card .group-toggle:hover { background: var(--bg-sunken); color: var(--ink); }
  .group-items { margin-top: .7em; border-top: 1px solid var(--rule-soft); padding-top: .6em; font-size: .78rem; max-height: 220px; overflow-y: auto; }
  .group-items .g-row { display: flex; justify-content: space-between; gap: .6em; padding: .3em 0; border-bottom: 1px dashed var(--rule-soft); color: var(--ink-muted); }
  .group-items .g-row:last-child { border-bottom: none; }
  .group-items .g-row span:first-child { color: var(--ink); }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>Revenue Recovery</h1>
      <div class="sub">Payment-failure detection &amp; recovery, live against the real pipeline</div>
    </div>
    <div class="business-badge" id="business-badge">business: demo · seed 42</div>
  </header>

  <div class="actions">
    <button class="secondary" id="btn-populate" onclick="populate()">
      <span class="spinner"></span><span class="label">&#8635; Populate fresh data</span>
    </button>
    <button class="primary" id="btn-recover" onclick="launchRecovery()">
      <span class="spinner"></span><span class="label">&#9654; Launch recovery actions</span>
    </button>
  </div>
  <div class="status-line" id="status-line"><span class="dot"></span><span id="status-text">Ready.</span></div>

  <div class="kpi-grid" id="kpi-grid"></div>

  <section class="panel">
    <div class="panel-head">
      <h2>Recovery Explanation</h2>
    </div>
    <div class="explain-body" id="explain-groups">
      <div class="narrative-text">Launch recovery to see every transaction grouped by what happened to it -- recovered, actioned and awaiting a response, suppressed for compliance, or stopped/held -- each with its own explanation of why (AI-generated when a key is configured, a deterministic template otherwise, always labeled).</div>
    </div>
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Activity</h2></div>
    <div class="log" id="log" style="border-radius:0;"></div>
  </section>

  <section class="panel">
    <div class="panel-head">
      <h2>Transactions</h2>
      <span class="count" id="tx-count"></span>
    </div>
    <div class="table-scroll" id="tx-table"></div>
  </section>
</div>

<script>
const fmtINR = (minor) => {
  if (minor === null || minor === undefined) return '—';
  const rupees = Math.trunc(minor / 100);
  const sign = rupees < 0 ? '-' : '';
  const s = Math.abs(rupees).toString();
  let grouped;
  if (s.length <= 3) grouped = s;
  else {
    const last3 = s.slice(-3);
    let rest = s.slice(0, -3);
    const parts = [];
    while (rest.length > 2) { parts.unshift(rest.slice(-2)); rest = rest.slice(0, -2); }
    if (rest) parts.unshift(rest);
    grouped = parts.join(',') + ',' + last3;
  }
  return sign + '₹' + grouped;
};

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s === null || s === undefined ? '' : String(s);
  return div.innerHTML;
}

function log(msg, cls) {
  const el = document.getElementById('log');
  const line = document.createElement('div');
  line.className = 'line' + (cls ? ' ' + cls : '');
  const t = new Date().toLocaleTimeString();
  line.innerHTML = '<span class="t">' + t + '</span>' + msg;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

function setStatus(text, mode) {
  const line = document.getElementById('status-line');
  line.className = 'status-line' + (mode ? ' ' + mode : '');
  document.getElementById('status-text').textContent = text;
}

function setButtonLoading(id, loading) {
  const btn = document.getElementById(id);
  btn.disabled = loading;
  btn.classList.toggle('loading', loading);
}

function renderKpis(k) {
  const grid = document.getElementById('kpi-grid');
  const lift = k.lift_pp;
  const liftClass = lift > 0 ? 'up' : (lift < 0 ? 'down' : '');
  const netClass = k.net_minor > 0 ? 'up' : (k.net_minor < 0 ? 'down' : '');
  grid.innerHTML = `
    <div class="kpi"><div class="label">Total at risk</div><div class="value">${fmtINR(k.total_at_risk_minor)}</div><div class="foot">${k.total_items} loss records</div></div>
    <div class="kpi"><div class="label">Treatment recovered</div><div class="value accent">${fmtINR(k.treatment.recovered_minor)}</div><div class="foot">${k.treatment.recovered_strong} of ${k.treatment.total} · ${k.treatment.rate_pct}%</div></div>
    <div class="kpi"><div class="label">Holdout recovered</div><div class="value">${fmtINR(k.holdout.recovered_minor)}</div><div class="foot">${k.holdout.recovered_strong} of ${k.holdout.total} · ${k.holdout.rate_pct}%</div></div>
    <div class="kpi"><div class="label">Lift</div><div class="value ${liftClass}">${lift > 0 ? '+' : ''}${lift}pp</div><div class="foot">treatment vs. holdout</div></div>
    <div class="kpi"><div class="label">Net recovered</div><div class="value ${netClass}">${fmtINR(k.net_minor)}</div><div class="foot">vs. organic baseline</div></div>
    <div class="kpi"><div class="label">Not actioned</div><div class="value">${k.suppressed_compliance + k.stopped_by_rules + k.held_for_approval}</div><div class="foot">${k.suppressed_compliance} compliance · ${k.stopped_by_rules} rules · ${k.held_for_approval} held</div></div>
  `;
}

function renderTransactions(rows) {
  document.getElementById('tx-count').textContent = rows.length + ' shown';
  const wrap = document.getElementById('tx-table');
  if (!rows.length) {
    wrap.innerHTML = '<div class="empty-state"><div class="big">&#128193;</div>No transactions yet. Click &ldquo;Populate fresh data&rdquo; to generate some.</div>';
    return;
  }
  const rowsHtml = rows.map(r => `
    <tr>
      <td class="mono">${r.payment_id.slice(0, 8)}</td>
      <td>${escapeHtml(r.customer_email) || '—'}</td>
      <td class="num">${fmtINR(r.amount_minor)}</td>
      <td>${escapeHtml(r.method) || '—'}</td>
      <td>${escapeHtml(r.issuer_bank) || '—'}</td>
      <td><span class="pill ${r.status}">${r.status}</span></td>
      <td>${escapeHtml(r.failure_reason) || '—'}</td>
      <td class="mono">${r.attempt_number}</td>
      <td class="mono">${(r.initiated_at || '').replace('T', ' ').slice(0, 19)}</td>
      <td>${r.at_risk_id ? `<button class="explain-btn" onclick="explainTransaction('${r.at_risk_id}', this)">Explain</button>` : '—'}</td>
    </tr>`).join('');
  wrap.innerHTML = `<table>
    <thead><tr><th>Payment</th><th>Customer</th><th>Amount</th><th>Method</th><th>Bank</th><th>Status</th><th>Failure reason</th><th>Attempt</th><th>Initiated</th><th>Explain</th></tr></thead>
    <tbody>${rowsHtml}</tbody>
  </table>`;
}

async function explainTransaction(atRiskId, btn) {
  const row = btn.closest('tr');
  const colCount = row.children.length;
  btn.disabled = true;
  btn.textContent = 'Explaining…';
  try {
    const r = await fetch('/dashboard/api/explain-transaction', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ at_risk_id: atRiskId }),
    });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    const badgeHtml = data.is_fallback
      ? '<span class="badge badge-fallback">Template fallback</span>'
      : '<span class="badge badge-ai">AI explanation</span>';
    const detailRow = document.createElement('tr');
    detailRow.className = 'explain-detail-row';
    detailRow.innerHTML = `<td colspan="${colCount}"><div class="row-narrative">${badgeHtml}<br>${escapeHtml(data.narrative)}</div></td>`;
    row.after(detailRow);
    btn.remove();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Explain';
    log('Explaining that transaction failed: ' + e.message, 'warn');
  }
}

const GROUP_ORDER = ['recovered', 'executed_pending', 'suppressed', 'stopped_held'];

function renderGroups(groups) {
  const wrap = document.getElementById('explain-groups');
  wrap.innerHTML = '<div class="group-grid">' + GROUP_ORDER.map(key => {
    const g = groups[key];
    if (!g) return '';
    const badgeHtml = g.is_fallback
      ? '<span class="badge badge-fallback">Template fallback</span>'
      : '<span class="badge badge-ai">AI explanation</span>';
    const itemsHtml = (g.items || []).map(it => {
      const who = escapeHtml(it.customer_email) || (it.payment_id || '—');
      const detailBits = [];
      if (it.channel) detailBits.push(escapeHtml(it.channel));
      if (it.suppressed_reason) detailBits.push(escapeHtml(it.suppressed_reason));
      if (it.outcome) detailBits.push(escapeHtml(it.outcome));
      return `<div class="g-row"><span>${who}</span><span>${fmtINR(it.amount_minor)}${detailBits.length ? ' · ' + detailBits.join(' · ') : ''}</span></div>`;
    }).join('');
    const toggleId = 'group-items-' + key;
    return `
      <div class="group-card">
        <div class="group-head"><span class="group-title">${escapeHtml(g.group_name)}</span>${badgeHtml}</div>
        <div class="group-meta">${g.count} item${g.count === 1 ? '' : 's'} · ₹${g.total_at_risk_rupees} at risk</div>
        <div class="group-narrative">${escapeHtml(g.narrative)}</div>
        ${g.items && g.items.length ? `
          <button class="group-toggle" onclick="const el = document.getElementById('${toggleId}'); el.style.display = el.style.display === 'none' ? 'block' : 'none';">Show / hide transactions</button>
          <div class="group-items" id="${toggleId}" style="display:none;">${itemsHtml}</div>
        ` : ''}
      </div>`;
  }).join('') + '</div>';
}

async function refreshKpis() {
  const r = await fetch('/dashboard/api/kpis');
  renderKpis(await r.json());
}
async function refreshTransactions() {
  const r = await fetch('/dashboard/api/transactions');
  renderTransactions(await r.json());
}
async function refreshAll() { await Promise.all([refreshKpis(), refreshTransactions()]); }

async function populate() {
  setButtonLoading('btn-populate', true);
  document.getElementById('btn-recover').disabled = true;
  setStatus('Populating fresh transaction data — generating, ingesting, normalizing, detecting risk…', 'busy');
  log('Populate started: wiping old data, generating a brand-new random backlog…');
  try {
    const r = await fetch('/dashboard/api/populate', { method: 'POST' });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    log(`Generated with content seed <b>${data.content_seed}</b> in ${data.elapsed_seconds}s — ${data.imports.inserted} events ingested, ${data.drain.dead_letter} dead-lettered (by design), ${data.newly_abandoned_checkouts} checkouts abandoned, ${data.newly_overdue_invoices} invoices overdue.`, 'ok');
    setStatus(`Fresh data ready (seed ${data.content_seed}, generated in ${data.elapsed_seconds}s). Ready to launch recovery.`, 'ok');
    await refreshAll();
  } catch (e) {
    log('Populate failed: ' + e.message, 'warn');
    setStatus('Populate failed — see activity log.', '');
  } finally {
    setButtonLoading('btn-populate', false);
    document.getElementById('btn-recover').disabled = false;
  }
}

async function launchRecovery() {
  setButtonLoading('btn-recover', true);
  document.getElementById('btn-populate').disabled = true;
  setStatus('Launching recovery actions — deciding, bounding, dispatching nudges and retries…', 'busy');
  log('Recovery launch started: running policy → bounds → channels over every open loss…');
  try {
    const r = await fetch('/dashboard/api/launch-recovery', { method: 'POST' });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    log(`Recovery launched ✓ — ${data.actions_executed} actions executed, ${data.actions_suppressed} suppressed, ${data.actions_stopped} stopped/held.`, 'ok');
    setStatus('Recovery launched. Waiting for simulated customer responses…', 'busy');
    await refreshAll();

    let secondsLeft = 5;
    const countdown = setInterval(() => {
      secondsLeft -= 1;
      if (secondsLeft > 0) setStatus(`Recovery launched. Simulated replies arriving in ${secondsLeft}s…`, 'busy');
    }, 1000);

    setTimeout(async () => {
      clearInterval(countdown);
      setStatus('Simulating customer responses…', 'busy');
      log('Simulating customer responses (this is the simulated environment standing in for real webhook replies)…');
      try {
        const r2 = await fetch('/dashboard/api/simulate-replies', { method: 'POST' });
        if (!r2.ok) throw new Error(await r2.text());
        const sim = await r2.json();
        const recoveredCount = sim.simulate.recovered;
        log(`Simulated replies received ✓ — ${recoveredCount} customers responded, ${sim.attribution.strong} matched with STRONG attribution.`, 'ok');
        await refreshAll();

        log('Grouping every transaction from this batch by what happened to it, and explaining each group (AI first, deterministic template as fallback)…');
        try {
          const gr = await fetch('/dashboard/api/explain-batch-groups', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ batch_id: data.batch_id }),
          });
          if (!gr.ok) throw new Error(await gr.text());
          const groups = await gr.json();
          renderGroups(groups);
          const fallbackCount = Object.values(groups).filter(g => g.is_fallback && g.count > 0).length;
          log(fallbackCount > 0
            ? `Group explanations ready — ${fallbackCount} of 4 used the template fallback (no AI key, or the AI call/verification did not succeed).`
            : 'Group explanations ready ✓ — all AI-generated.', 'ok');
        } catch (e) {
          log('Group explanation generation failed: ' + e.message, 'warn');
        }

        const kr = await fetch('/dashboard/api/kpis');
        const k = await kr.json();
        setStatus(`Recovery complete — ${fmtINR(k.treatment.recovered_minor)} recovered so far this run.`, 'ok');
      } catch (e) {
        log('Simulating replies failed: ' + e.message, 'warn');
        setStatus('Simulated replies failed — see activity log.', '');
      } finally {
        setButtonLoading('btn-recover', false);
        document.getElementById('btn-populate').disabled = false;
      }
    }, 5000);
  } catch (e) {
    log('Recovery launch failed: ' + e.message, 'warn');
    setStatus('Recovery launch failed — see activity log.', '');
    setButtonLoading('btn-recover', false);
    document.getElementById('btn-populate').disabled = false;
  }
}

refreshAll();
log('Dashboard loaded.');
</script>
</body>
</html>
"""
