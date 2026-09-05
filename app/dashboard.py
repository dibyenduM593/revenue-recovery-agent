"""The interactive control-panel dashboard: populate data, see the

transaction list as a business owner would, launch recovery actions, and
watch the (simulated) customer responses come back with real KPIs
updating live. Plain server-rendered HTML + vanilla JS (fetch, no build
step, no framework) -- deliberately not a React SPA, while still being
a real, driven, stateful interface rather than a
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


@router.get("/dashboard/api/transactions/b2c")
def api_transactions_b2c():
    return JSONResponse(orchestrator.get_transactions_b2c())


@router.get("/dashboard/api/transactions/b2b")
def api_transactions_b2b():
    return JSONResponse(orchestrator.get_transactions_b2b())


@router.get("/dashboard/api/summary/b2c")
def api_summary_b2c():
    return JSONResponse(orchestrator.get_b2c_summary())


@router.get("/dashboard/api/summary/b2b")
def api_summary_b2b():
    return JSONResponse(orchestrator.get_b2b_summary())


@router.get("/dashboard/api/human-review")
def api_human_review():
    return JSONResponse(orchestrator.get_human_review_queue())


@router.get("/dashboard/api/predictions")
def api_predictions(customer_type: str | None = None):
    return JSONResponse(orchestrator.get_predictions(customer_type))


@router.post("/dashboard/api/launch-recovery")
def api_launch_recovery():
    return JSONResponse(orchestrator.launch_recovery())


@router.post("/dashboard/api/ensure-live-poll")
def api_ensure_live_poll():
    return JSONResponse(orchestrator.ensure_live_poll())


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

  .tabs { display: flex; gap: .4em; border-bottom: 1px solid var(--rule); margin-bottom: 1.6em; }
  .tab-btn {
    font-family: var(--font); font-size: .9rem; font-weight: 600; color: var(--ink-muted);
    background: none; border: none; border-bottom: 2px solid transparent; padding: .7em .3em; margin-bottom: -1px;
    cursor: pointer;
  }
  .tab-btn:hover { color: var(--ink); }
  .tab-btn.active { color: var(--accent-strong); border-bottom-color: var(--accent); }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }

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

  /* Predictions panel */
  .predict-grid { display: grid; grid-template-columns: minmax(220px, 1fr) 2fr; gap: 1.8em; padding: 1.3em; align-items: center; }
  @media (max-width: 720px) { .predict-grid { grid-template-columns: 1fr; } }
  .donut-wrap { display: flex; flex-direction: column; align-items: center; gap: .3em; }
  .donut-wrap svg { display: block; }
  .donut-center-value { font-family: var(--mono); font-size: 1.5rem; font-weight: 700; }
  .donut-center-label { font-size: .74rem; color: var(--ink-muted); text-align: center; max-width: 150px; }
  .legend { display: flex; gap: 1.2em; flex-wrap: wrap; justify-content: center; margin-top: .5em; }
  .legend-item { display: flex; align-items: center; gap: .4em; font-size: .8rem; color: var(--ink-muted); }
  .legend-dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }

  .segment-bars { display: flex; flex-direction: column; gap: .65em; }
  .segbar-row { display: grid; grid-template-columns: 130px 1fr 44px; align-items: center; gap: .7em; font-size: .82rem; }
  .segbar-row .seg-label { color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .segbar-track { background: var(--bg-sunken); border-radius: 6px; height: 12px; overflow: hidden; }
  .segbar-fill { height: 100%; border-radius: 6px; background: linear-gradient(90deg, var(--accent), var(--accent-strong)); }
  .segbar-pct { font-family: var(--mono); font-size: .78rem; color: var(--ink-muted); text-align: right; }

  .lift-chart { display: flex; align-items: flex-end; gap: 2.2em; padding: 1.3em 1.5em .5em; height: 160px; }
  .lift-bar-col { display: flex; flex-direction: column; align-items: center; gap: .5em; flex: 1; height: 100%; justify-content: flex-end; }
  .lift-bar { width: 56px; border-radius: 8px 8px 0 0; transition: height .4s ease; }
  .lift-bar.treatment { background: linear-gradient(180deg, var(--accent), var(--accent-strong)); }
  .lift-bar.holdout { background: var(--ink-faint); opacity: .55; }
  .lift-bar-value { font-family: var(--mono); font-weight: 700; font-size: .95rem; }
  .lift-bar-label { font-size: .78rem; color: var(--ink-muted); }

  .pill.likelihood-high { background: var(--accent-soft); color: var(--accent-strong); }
  .pill.likelihood-medium { background: var(--amber-soft); color: var(--amber); }
  .pill.likelihood-low { background: var(--red-soft); color: var(--red); }

  .pill.PAID { background: var(--accent-soft); color: var(--accent-strong); }
  .pill.ISSUED { background: var(--amber-soft); color: var(--amber); }
  .pill.EXPIRED { background: var(--red-soft); color: var(--red); }
  .score-pct { font-family: var(--mono); font-weight: 700; }
  .type-tag { font-family: var(--mono); font-size: .7rem; font-weight: 700; padding: .15em .5em; border-radius: 4px; letter-spacing: .03em; }
  .type-tag.B2C { background: var(--blue-soft); color: var(--blue); }
  .type-tag.B2B { background: var(--amber-soft); color: var(--amber); }

  .live-demo-form { display: flex; gap: .7em; flex-wrap: wrap; align-items: center; padding: 1.1em 1.3em; }
  .live-demo-form input[type="tel"], .live-demo-form select {
    font-family: var(--font); font-size: .9rem; padding: .6em .8em; border-radius: 7px;
    border: 1px solid var(--rule); background: var(--bg-raised); color: var(--ink); min-width: 150px;
  }
  .live-demo-form select { min-width: 220px; }
  .live-demo-form label.checkbox { display: flex; align-items: center; gap: .4em; font-size: .82rem; color: var(--ink-muted); }
  .live-demo-status { padding: 0 1.3em 1.1em; font-size: .85rem; color: var(--ink-muted); }
  .live-demo-result { padding: 0 1.3em 1.3em; }
  .live-demo-card { background: var(--bg-sunken); border-radius: 8px; padding: 1em 1.2em; font-size: .86rem; }
  .live-demo-card .row { display: flex; justify-content: space-between; padding: .3em 0; border-bottom: 1px dashed var(--rule); }
  .live-demo-card .row:last-child { border-bottom: none; }
  .live-demo-card .row span:first-child { color: var(--ink-muted); }
  .badge-real { background: var(--red-soft); color: var(--red); }
  .badge-sim { background: var(--blue-soft); color: var(--blue); }
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

  <div class="tabs">
    <button class="tab-btn active" data-tab="main" onclick="showTab('main')">Main</button>
    <button class="tab-btn" data-tab="b2c" onclick="showTab('b2c')">B2C</button>
    <button class="tab-btn" data-tab="b2b" onclick="showTab('b2b')">B2B</button>
    <button class="tab-btn" data-tab="human" onclick="showTab('human')">Human Review</button>
  </div>

  <div class="tab-panel active" data-tab="main">
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
        <h2>Try it on your own phone</h2>
        <span class="count">real call + WhatsApp, only for a verified number</span>
      </div>
      <div class="live-demo-form">
        <input type="tel" id="live-demo-phone" placeholder="+91XXXXXXXXXX" value="+91">
        <select id="live-demo-scenario"></select>
        <label class="checkbox"><input type="checkbox" id="live-demo-consent"> This is my own number</label>
        <button class="secondary" id="btn-plant" onclick="plantLiveDemo()">
          <span class="spinner"></span><span class="label">Plant this transaction</span>
        </button>
      </div>
      <div class="live-demo-status" id="live-demo-status">Enter a phone number and pick why the payment failed. This goes through the real pipeline -- normalization, risk detection, scoring -- exactly like the synthetic backlog, just for one item you control.</div>
      <div class="live-demo-result" id="live-demo-result" style="display:none;"></div>
    </section>

    <section class="panel" id="lift-panel" style="display:none;">
      <div class="panel-head"><h2>Actioned vs. left alone</h2></div>
      <div class="lift-chart" id="lift-chart"></div>
    </section>

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
  </div>

  <div class="tab-panel" data-tab="b2c">
    <div class="kpi-grid" id="b2c-kpi-grid"></div>

    <section class="panel" id="b2c-segments-panel" style="display:none;">
      <div class="panel-head"><h2>By customer segment</h2></div>
      <div id="b2c-segments-grid" style="display:flex; justify-content:center; padding:1.3em;"></div>
    </section>

    <section class="panel" id="b2c-predictions-panel" style="display:none;">
      <div class="panel-head">
        <h2>Recovery Predictions</h2>
        <span class="count" id="b2c-predict-model-label"></span>
      </div>
      <div class="predict-grid" id="b2c-predict-grid"></div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>B2C Transactions</h2>
        <span class="count" id="b2c-tx-count"></span>
      </div>
      <div class="table-scroll" id="b2c-tx-table"></div>
    </section>
  </div>

  <div class="tab-panel" data-tab="b2b">
    <div class="kpi-grid" id="b2b-kpi-grid"></div>

    <section class="panel" id="b2b-segments-panel" style="display:none;">
      <div class="panel-head"><h2>By customer segment</h2></div>
      <div id="b2b-segments-grid" style="display:flex; justify-content:center; padding:1.3em;"></div>
    </section>

    <section class="panel" id="b2b-overdue-panel" style="display:none;">
      <div class="panel-head"><h2>Days overdue</h2></div>
      <div id="b2b-overdue-chart" style="padding:1.2em 1.3em; overflow-x:auto;"></div>
    </section>

    <section class="panel" id="b2b-predictions-panel" style="display:none;">
      <div class="panel-head">
        <h2>Recovery Predictions</h2>
        <span class="count" id="b2b-predict-model-label"></span>
      </div>
      <div class="predict-grid" id="b2b-predict-grid"></div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>B2B Transactions</h2>
        <span class="count" id="b2b-tx-count"></span>
      </div>
      <div class="table-scroll" id="b2b-tx-table"></div>
    </section>
  </div>

  <div class="tab-panel" data-tab="human">
    <div class="kpi-grid" id="human-kpi-grid"></div>

    <section class="panel" id="human-reason-panel" style="display:none;">
      <div class="panel-head"><h2>By reason</h2></div>
      <div id="human-reason-grid" style="display:flex; justify-content:center; padding:1.3em;"></div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Human Review Queue</h2>
        <span class="count" id="human-queue-count"></span>
      </div>
      <div class="table-scroll" id="human-queue-table"></div>
    </section>
  </div>
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
  if (!btn) return;  // e.g. btn-call-me, which the live-demo result render can replace mid-flight
  btn.disabled = loading;
  btn.classList.toggle('loading', loading);
}

function showTab(name) {
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.toggle('active', panel.dataset.tab === name));
}

function renderKpis(k) {
  const grid = document.getElementById('kpi-grid');
  const lift = k.lift_pp;
  const liftClass = lift > 0 ? 'up' : (lift < 0 ? 'down' : '');
  grid.innerHTML = `
    <div class="kpi"><div class="label">Total at risk</div><div class="value">${fmtINR(k.total_at_risk_minor)}</div><div class="foot">${k.total_items} loss records</div></div>
    <div class="kpi"><div class="label">Treatment recovered</div><div class="value accent">${fmtINR(k.treatment.recovered_minor)}</div><div class="foot">${k.treatment.recovered_strong} of ${k.treatment.total} · ${k.treatment.rate_pct}%</div></div>
    <div class="kpi"><div class="label">Holdout recovered</div><div class="value">${fmtINR(k.holdout.recovered_minor)}</div><div class="foot">${k.holdout.recovered_strong} of ${k.holdout.total} · ${k.holdout.rate_pct}%</div></div>
    <div class="kpi"><div class="label">Lift</div><div class="value ${liftClass}">${lift > 0 ? '+' : ''}${lift}pp</div><div class="foot">treatment vs. holdout</div></div>
    <div class="kpi"><div class="label">Confidently recoverable</div><div class="value accent">${fmtINR(k.confidently_recoverable_minor)}</div><div class="foot">open or in-recovery, &gt;80% chance</div></div>
    <div class="kpi"><div class="label">In human hands</div><div class="value">${fmtINR(k.in_human_hands_minor)}</div><div class="foot">awaiting a human decision</div></div>
    <div class="kpi"><div class="label">Not actioned</div><div class="value">${k.suppressed_compliance + k.stopped_by_rules}</div><div class="foot">${k.suppressed_compliance} compliance · ${k.stopped_by_rules} rules</div></div>
    <div class="kpi"><div class="label">Total recovered</div><div class="value accent">${fmtINR(k.total_recovered_minor)}</div><div class="foot">every source, all-time -- not a measurement, just the total</div></div>
  `;
}

const LIKELIHOOD_LABEL = { high: 'High', medium: 'Medium', low: 'Low' };

function likelihoodPill(level) {
  if (!level) return '—';
  return `<span class="pill likelihood-${level}">${LIKELIHOOD_LABEL[level]}</span>`;
}

function renderB2CTransactions(rows) {
  document.getElementById('b2c-tx-count').textContent = rows.length ? `${rows.length} payments` : '';
  const wrap = document.getElementById('b2c-tx-table');
  if (!rows.length) { wrap.innerHTML = '<div class="empty-state">No B2C transactions yet. Click &ldquo;Populate fresh data&rdquo; on the Main tab.</div>'; return; }
  const rowsHtml = rows.map(r => `
    <tr>
      <td class="mono">${r.payment_id.slice(0, 8)}</td>
      <td>${escapeHtml(r.customer_email) || '—'}</td>
      <td class="num">${fmtINR(r.amount_minor)}</td>
      <td>${escapeHtml(r.method) || '—'}</td>
      <td>${escapeHtml(r.issuer_bank) || '—'}</td>
      <td><span class="pill ${r.status}">${r.status}</span></td>
      <td>${escapeHtml(r.failure_reason) || '—'}</td>
      <td>${likelihoodPill(r.recovery_likelihood)}</td>
      <td class="mono">${r.attempt_number}</td>
      <td class="mono">${(r.initiated_at || '').replace('T', ' ').slice(0, 19)}</td>
      <td>${r.at_risk_id ? `<button class="explain-btn" onclick="explainTransaction('${r.at_risk_id}', this)">Explain</button>` : '—'}</td>
    </tr>`).join('');
  wrap.innerHTML = `<table>
    <thead><tr><th>Payment</th><th>Customer</th><th>Amount</th><th>Method</th><th>Bank</th><th>Status</th><th>Failure reason</th><th>Chance of recovery</th><th>Attempt</th><th>Initiated</th><th>Explain</th></tr></thead>
    <tbody>${rowsHtml}</tbody>
  </table>`;
}

function renderB2BTransactions(rows) {
  document.getElementById('b2b-tx-count').textContent = rows.length ? `${rows.length} invoices` : '';
  const wrap = document.getElementById('b2b-tx-table');
  if (!rows.length) { wrap.innerHTML = '<div class="empty-state">No B2B transactions yet. Click &ldquo;Populate fresh data&rdquo; on the Main tab.</div>'; return; }
  const rowsHtml = rows.map(r => `
    <tr>
      <td class="mono">${r.invoice_id.slice(0, 8)}</td>
      <td>${escapeHtml(r.customer_name || r.customer_email) || '—'}</td>
      <td class="num">${fmtINR(r.amount_minor)}</td>
      <td class="num">${r.days_overdue}</td>
      <td class="mono">${r.dunning_stage}</td>
      <td><span class="pill ${r.status}">${r.status}</span></td>
      <td>${likelihoodPill(r.recovery_likelihood)}</td>
      <td>${r.at_risk_id ? `<button class="explain-btn" onclick="explainTransaction('${r.at_risk_id}', this)">Explain</button>` : '—'}</td>
    </tr>`).join('');
  wrap.innerHTML = `<table>
    <thead><tr><th>Invoice</th><th>Customer</th><th>Amount</th><th>Days overdue</th><th>Dunning stage</th><th>Status</th><th>Chance of recovery</th><th>Explain</th></tr></thead>
    <tbody>${rowsHtml}</tbody>
  </table>`;
}

const SEGMENT_PALETTE = ['#1f5d50', '#96661a', '#4f5a86', '#a13c30', '#6b8e6f', '#8b5fa0', '#c17a3d', '#3d7a8e'];

function renderScopedKpis(gridId, s) {
  document.getElementById(gridId).innerHTML = `
    <div class="kpi"><div class="label">At risk</div><div class="value">${fmtINR(s.at_risk_minor)}</div><div class="foot">${s.count} loss records</div></div>
    <div class="kpi"><div class="label">Recovered</div><div class="value accent">${fmtINR(s.recovered_minor)}</div><div class="foot">${s.recovered_count} of ${s.count}</div></div>
    <div class="kpi"><div class="label">Recovery rate</div><div class="value">${s.recovery_rate_pct}%</div><div class="foot">of losses in this segment</div></div>
  `;
}

function renderSegmentsDonut(containerId, segments) {
  const el = document.getElementById(containerId);
  if (!segments || !segments.length) { el.innerHTML = '<div style="color:var(--ink-muted);">Not enough data yet.</div>'; return; }
  const total = segments.reduce((s, x) => s + x.at_risk_minor, 0);
  const donutSegs = segments.map((s, i) => ({ value: s.at_risk_minor, color: SEGMENT_PALETTE[i % SEGMENT_PALETTE.length] }));
  const donut = donutSvg(donutSegs, fmtINR(total), 'at risk, by segment');
  const legend = segments.map((s, i) => `
    <div class="legend-item"><span class="legend-dot" style="background:${SEGMENT_PALETTE[i % SEGMENT_PALETTE.length]}"></span>${escapeHtml(s.label)} — ${fmtINR(s.at_risk_minor)}</div>`).join('');
  el.innerHTML = `<div class="donut-wrap">${donut}<div class="legend">${legend}</div></div>`;
}

function barChartSvg(buckets) {
  const max = Math.max(...buckets.map(b => b.count), 1);
  const barH = 22, gap = 10, leftW = 56, chartW = 320, rightW = 34;
  const width = leftW + chartW + rightW;
  const height = buckets.length * (barH + gap) - gap;
  const rows = buckets.map((b, i) => {
    const y = i * (barH + gap);
    const w = b.count > 0 ? Math.max((b.count / max) * chartW, 3) : 0;
    return `
      <text x="${leftW - 8}" y="${y + barH / 2 + 4}" text-anchor="end" font-family="var(--mono)" font-size="11" fill="var(--ink-muted)">${escapeHtml(b.label)}d</text>
      <rect x="${leftW}" y="${y}" width="${chartW}" height="${barH}" rx="4" fill="var(--bg-sunken)"></rect>
      <rect x="${leftW}" y="${y}" width="${w}" height="${barH}" rx="4" fill="var(--amber)"></rect>
      <text x="${leftW + chartW + 8}" y="${y + barH / 2 + 4}" font-family="var(--mono)" font-size="11" fill="var(--ink-muted)">${b.count}</text>`;
  }).join('');
  return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" style="max-width:100%;">${rows}</svg>`;
}

const REASON_LABEL = {
  invoice_dunning_exhausted: 'Invoice dunning exhausted',
  do_not_honor_retries_exhausted: 'Possible false decline',
  value_threshold: 'Value threshold',
  escalate_human: 'Escalated to human',
};

function renderHumanKpis(h) {
  document.getElementById('human-kpi-grid').innerHTML = `
    <div class="kpi"><div class="label">In review</div><div class="value">${h.count}</div><div class="foot">tickets awaiting a human</div></div>
    <div class="kpi"><div class="label">Total value</div><div class="value">${fmtINR(h.total_at_risk_minor)}</div><div class="foot">at risk while under review</div></div>
    <div class="kpi"><div class="label">Avg. chance of recovery</div><div class="value">${Math.round(h.avg_score * 100)}%</div><div class="foot">across the queue</div></div>
  `;
}

function renderHumanReasonChart(byReason) {
  const panel = document.getElementById('human-reason-panel');
  const entries = Object.entries(byReason || {});
  if (!entries.length) { panel.style.display = 'none'; return; }
  panel.style.display = '';
  const total = entries.reduce((s, [, c]) => s + c, 0);
  const segs = entries.map(([reason, count], i) => ({ value: count, color: SEGMENT_PALETTE[i % SEGMENT_PALETTE.length] }));
  const donut = donutSvg(segs, total, 'tickets');
  const legend = entries.map(([reason, count], i) => `
    <div class="legend-item"><span class="legend-dot" style="background:${SEGMENT_PALETTE[i % SEGMENT_PALETTE.length]}"></span>${escapeHtml(REASON_LABEL[reason] || reason)} (${count})</div>`).join('');
  document.getElementById('human-reason-grid').innerHTML = `<div class="donut-wrap">${donut}<div class="legend">${legend}</div></div>`;
}

function renderHumanQueue(items) {
  document.getElementById('human-queue-count').textContent = items.length ? `${items.length} tickets` : '';
  const wrap = document.getElementById('human-queue-table');
  if (!items.length) { wrap.innerHTML = '<div class="empty-state">Nothing awaiting human review right now.</div>'; return; }
  const rowsHtml = items.map(it => `
    <tr>
      <td class="mono">${it.at_risk_id.slice(0, 8)}</td>
      <td><span class="type-tag ${it.customer_type || ''}">${it.customer_type || '—'}</span></td>
      <td>${escapeHtml(it.entity_type)}</td>
      <td class="num">${fmtINR(it.value_minor)}</td>
      <td class="score-pct">${Math.round(it.predicted_score * 100)}%</td>
      <td>${escapeHtml(REASON_LABEL[it.reason] || it.reason)}</td>
      <td class="mono">${(it.decided_at || '').replace('T', ' ').slice(0, 19)}</td>
      <td><button class="explain-btn" onclick="explainTransaction('${it.at_risk_id}', this)">Explain</button></td>
    </tr>`).join('');
  wrap.innerHTML = `<table>
    <thead><tr><th>Item</th><th>Type</th><th>Entity</th><th>Value</th><th>Chance of recovery</th><th>Reason</th><th>Decided</th><th>Explain</th></tr></thead>
    <tbody>${rowsHtml}</tbody>
  </table>`;
}

function donutSvg(segments, centerValue, centerLabel) {
  // segments: [{value, color}], drawn clockwise starting at 12 o'clock.
  const size = 150, r = 58, cx = size / 2, cy = size / 2, stroke = 20;
  const total = segments.reduce((s, seg) => s + seg.value, 0) || 1;
  let angle = -90;
  const circumference = 2 * Math.PI * r;
  const arcs = segments.filter(s => s.value > 0).map(seg => {
    const frac = seg.value / total;
    const dash = frac * circumference;
    const gap = circumference - dash;
    const rotate = angle;
    angle += frac * 360;
    return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${seg.color}" stroke-width="${stroke}"
      stroke-dasharray="${dash} ${gap}" transform="rotate(${rotate} ${cx} ${cy})" stroke-linecap="butt"></circle>`;
  }).join('');
  return `
    <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="var(--bg-sunken)" stroke-width="${stroke}"></circle>
      ${arcs}
      <text x="${cx}" y="${cy - 3}" text-anchor="middle" font-family="var(--mono)" font-size="22" font-weight="700" fill="var(--ink)">${centerValue}</text>
      <text x="${cx}" y="${cy + 16}" text-anchor="middle" font-size="9" fill="var(--ink-muted)">${centerLabel}</text>
    </svg>`;
}

function renderPredictionsPanel(prefix, p) {
  const panel = document.getElementById(prefix + '-predictions-panel');
  if (!p.scored_count) { panel.style.display = 'none'; return; }
  panel.style.display = '';
  document.getElementById(prefix + '-predict-model-label').textContent = p.model_label;

  const colors = { high: '#1f5d50', medium: '#96661a', low: '#a13c30' };
  const donut = donutSvg(
    [
      { value: p.buckets.high, color: colors.high },
      { value: p.buckets.medium, color: colors.medium },
      { value: p.buckets.low, color: colors.low },
    ],
    Math.round(p.avg_score * 100) + '%',
    'average chance of recovery'
  );
  const legend = `
    <div class="legend">
      <div class="legend-item"><span class="legend-dot" style="background:${colors.high}"></span>High (${p.buckets.high})</div>
      <div class="legend-item"><span class="legend-dot" style="background:${colors.medium}"></span>Medium (${p.buckets.medium})</div>
      <div class="legend-item"><span class="legend-dot" style="background:${colors.low}"></span>Low (${p.buckets.low})</div>
    </div>`;

  const maxSeg = Math.max(...p.segments.map(s => s.avg_score), 0.01);
  const segRows = p.segments.map(s => `
    <div class="segbar-row">
      <div class="seg-label">${escapeHtml(s.label)}</div>
      <div class="segbar-track"><div class="segbar-fill" style="width:${(s.avg_score / maxSeg * 100).toFixed(0)}%"></div></div>
      <div class="segbar-pct">${Math.round(s.avg_score * 100)}%</div>
    </div>`).join('');

  document.getElementById(prefix + '-predict-grid').innerHTML = `
    <div class="donut-wrap">${donut}${legend}</div>
    <div>
      <div style="font-size:.82rem;color:var(--ink-muted);margin-bottom:.9em;">
        Average chance of recovery by customer group, out of ${p.scored_count.toLocaleString()} at-risk payments scored.
      </div>
      <div class="segment-bars">${segRows || '<div class="segbar-row">Not enough data yet.</div>'}</div>
    </div>`;
}

function renderLiftChart(k) {
  const panel = document.getElementById('lift-panel');
  if (!k.treatment.total && !k.holdout.total) { panel.style.display = 'none'; return; }
  panel.style.display = '';
  const maxRate = Math.max(k.treatment.rate_pct, k.holdout.rate_pct, 1);
  const tHeight = Math.max((k.treatment.rate_pct / maxRate) * 120, 4);
  const hHeight = Math.max((k.holdout.rate_pct / maxRate) * 120, 4);
  document.getElementById('lift-chart').innerHTML = `
    <div class="lift-bar-col">
      <div class="lift-bar-value">${k.treatment.rate_pct}%</div>
      <div class="lift-bar treatment" style="height:${tHeight}px"></div>
      <div class="lift-bar-label">We reached out<br>(${k.treatment.total} payments)</div>
    </div>
    <div class="lift-bar-col">
      <div class="lift-bar-value">${k.holdout.rate_pct}%</div>
      <div class="lift-bar holdout" style="height:${hHeight}px"></div>
      <div class="lift-bar-label">Left alone, for comparison<br>(${k.holdout.total} payments)</div>
    </div>
    <div style="align-self:center;font-size:.85rem;color:var(--ink-muted);max-width:220px;">
      Reaching out recovered <b style="color:var(--ink)">${k.lift_pp > 0 ? '+' : ''}${k.lift_pp} percentage points</b> more than doing nothing would have.
    </div>`;
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
  const k = await r.json();
  renderKpis(k);
  renderLiftChart(k);
}

async function refreshB2C() {
  const [txRes, sumRes, predRes] = await Promise.all([
    fetch('/dashboard/api/transactions/b2c'),
    fetch('/dashboard/api/summary/b2c'),
    fetch('/dashboard/api/predictions?customer_type=B2C'),
  ]);
  renderB2CTransactions(await txRes.json());
  const summary = await sumRes.json();
  renderScopedKpis('b2c-kpi-grid', summary);
  const segPanel = document.getElementById('b2c-segments-panel');
  if (summary.segments && summary.segments.length) { segPanel.style.display = ''; renderSegmentsDonut('b2c-segments-grid', summary.segments); }
  else segPanel.style.display = 'none';
  renderPredictionsPanel('b2c', await predRes.json());
}

async function refreshB2B() {
  const [txRes, sumRes, predRes] = await Promise.all([
    fetch('/dashboard/api/transactions/b2b'),
    fetch('/dashboard/api/summary/b2b'),
    fetch('/dashboard/api/predictions?customer_type=B2B'),
  ]);
  renderB2BTransactions(await txRes.json());
  const summary = await sumRes.json();
  renderScopedKpis('b2b-kpi-grid', summary);
  const segPanel = document.getElementById('b2b-segments-panel');
  if (summary.segments && summary.segments.length) { segPanel.style.display = ''; renderSegmentsDonut('b2b-segments-grid', summary.segments); }
  else segPanel.style.display = 'none';
  const overduePanel = document.getElementById('b2b-overdue-panel');
  const buckets = summary.days_overdue_buckets || [];
  if (buckets.some(b => b.count > 0)) { overduePanel.style.display = ''; document.getElementById('b2b-overdue-chart').innerHTML = barChartSvg(buckets); }
  else overduePanel.style.display = 'none';
  renderPredictionsPanel('b2b', await predRes.json());
}

async function refreshHuman() {
  const r = await fetch('/dashboard/api/human-review');
  const data = await r.json();
  renderHumanKpis(data);
  renderHumanReasonChart(data.by_reason);
  renderHumanQueue(data.items);
}

async function refreshAll() { await Promise.all([refreshKpis(), refreshB2C(), refreshB2B(), refreshHuman()]); }

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
    if (data.live_demo && data.live_demo.length) {
      log(`Live demo: firing ${data.live_demo.length} planted item(s)…`);
      data.live_demo.forEach(renderLiveDemoOutcome);
    }
    startLiveKpiPolling();
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

async function refreshScenarios() {
  const sel = document.getElementById('live-demo-scenario');
  try {
    const r = await fetch('/dashboard/api/live-demo/scenarios');
    const data = await r.json();
    sel.innerHTML = data.scenarios.map(s => `<option value="${s.key}">${escapeHtml(s.label)}</option>`).join('');
  } catch (e) {
    sel.innerHTML = '<option value="">(failed to load scenarios)</option>';
  }
}

function renderLiveDemoResult(html) {
  const wrap = document.getElementById('live-demo-result');
  wrap.style.display = '';
  wrap.innerHTML = html;
}

async function plantLiveDemo() {
  const phone = document.getElementById('live-demo-phone').value.trim();
  const scenario = document.getElementById('live-demo-scenario').value;
  const consent = document.getElementById('live-demo-consent').checked;

  if (!/^\+\d{8,15}$/.test(phone)) {
    document.getElementById('live-demo-status').textContent = 'Enter a phone number in +<country><number> format, e.g. +919876543210.';
    return;
  }
  if (!consent) {
    document.getElementById('live-demo-status').textContent = 'Check "This is my own number" -- this will place a real call/message if the number is verified.';
    return;
  }

  setButtonLoading('btn-plant', true);
  document.getElementById('live-demo-status').textContent = 'Planting this transaction through the real pipeline…';
  log(`Live demo: planting a failed payment for ${phone}…`);
  try {
    const r = await fetch('/dashboard/api/live-demo/plant', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone_e164: phone, scenario }),
    });
    const data = await r.json();
    if (!data.ok) throw new Error(data.reason || 'plant failed');
    const realBadge = data.will_call_for_real
      ? '<span class="pill badge-real">Will call for real</span>'
      : '<span class="pill badge-sim">Simulated (number not verified)</span>';
    renderLiveDemoResult(`
      <div class="live-demo-card">
        <div class="row"><span>Scenario</span><span>${escapeHtml(data.scenario)}</span></div>
        <div class="row"><span>Amount</span><span>${fmtINR(data.amount_minor)}</span></div>
        <div class="row"><span>Customer</span><span>${escapeHtml(data.email)}</span></div>
        <div class="row"><span>Routing</span><span>${realBadge}</span></div>
      </div>
    `);
    document.getElementById('live-demo-status').textContent = 'Planted. Click "Launch recovery actions" above -- it fires the call + WhatsApp nudge along with the rest of the batch.';
    log(`Live demo: planted at_risk_id ${data.at_risk_id.slice(0, 8)} (${data.will_call_for_real ? 'real' : 'simulated'} routing). Click "Launch recovery actions" to fire it.`, 'ok');
    // plant() already ingested a real at-risk row through the pipeline --
    // total_items/total_at_risk_minor include it immediately, decided or
    // not. Without this the KPI tiles stay stale until something else
    // happens to trigger a refresh.
    await refreshAll();
  } catch (e) {
    document.getElementById('live-demo-status').textContent = 'Planting failed: ' + e.message;
    log('Live demo plant failed: ' + e.message, 'warn');
  } finally {
    setButtonLoading('btn-plant', false);
  }
}

function renderLiveDemoOutcome(data) {
  if (!data.queued) {
    renderLiveDemoResult(`
      <div class="live-demo-card">
        <div class="row"><span>Policy decided</span><span>${escapeHtml(data.action)}</span></div>
        <div class="row"><span>Result</span><span>${data.suppressed_reason ? escapeHtml(data.suppressed_reason) : 'no channel for this action'}</span></div>
      </div>
      <div class="live-demo-status" style="padding:.8em 0 0;">${data.explanation ? escapeHtml(data.explanation) : "Policy declined to contact -- that's a correct answer, not a bug."}</div>
    `);
    log(`Live demo: policy decided ${data.action}, nothing sent (${data.suppressed_reason || 'no channel'}).`, 'ok');
    return;
  }
  const sent = data.dispatch.sent, failed = data.dispatch.failed;
  renderLiveDemoResult(`
    <div class="live-demo-card">
      <div class="row"><span>Policy decided</span><span>${escapeHtml(data.action)}</span></div>
      <div class="row"><span>Routing</span><span>${data.for_real ? '<span class="pill badge-real">Real Twilio call + WhatsApp</span>' : '<span class="pill badge-sim">Simulated</span>'}</span></div>
      <div class="row"><span>Sent</span><span>${sent} of ${sent + failed + data.dispatch.suppressed}</span></div>
    </div>
  `);
  log(`Live demo: launched — ${sent} sent, ${failed} failed, ${data.dispatch.suppressed} suppressed at send time (${data.for_real ? 'real Twilio' : 'simulated'}).`, 'ok');
}

let _liveKpiPollTimer = null;

function startLiveKpiPolling() {
  if (_liveKpiPollTimer) return;  // already running -- clicking Launch again shouldn't stack up intervals
  _liveKpiPollTimer = setInterval(refreshKpis, 10000);
  log('Live detection on: KPIs will keep updating on their own as replies and nudge windows resolve.');
}

// Browsers throttle setInterval heavily in a backgrounded tab (Chrome can
// stretch a 10s interval to a minute or more once it's been hidden a
// while) -- the poll is still "running," it's just been starved by the
// tab being unfocused, which looks identical to frozen from here. Forcing
// one immediate refresh the moment the tab becomes visible again means
// switching back always shows the true current numbers without waiting
// for the throttled timer to catch up on its own.
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshKpis(); });

refreshAll();
refreshScenarios();
startLiveKpiPolling();
fetch('/dashboard/api/ensure-live-poll', { method: 'POST' }).catch(() => {});
log('Dashboard loaded.');
</script>
</body>
</html>
"""
