"""Day 11's "one server-rendered page": batch report -> per-item drilldown ->

click any rupee figure, see the provider's raw bytes and the generated
explanation. Plain server-rendered HTML, no template engine, no frontend
build step -- consistent with the plan's own "no new infrastructure" rule
and the explicit non-goal of a React SPA. Every page here is read-only.
"""

import html
import json
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.db import SessionLocal
from app.explain import explain
from app.models import RawEvent, RecoveryAttempt, RecoveryBatch, RevenueAtRisk
from app.recovery.report import build_report, render
from app.settings import DEMO_BUSINESS_ID

router = APIRouter()

_STYLE = """
<style>
  body { font-family: -apple-system, 'Segoe UI', sans-serif; max-width: 920px; margin: 2em auto; padding: 0 1em; color: #1b1f1c; background: #f7f8f5; }
  h1, h2 { font-weight: 600; }
  a { color: #1f5d50; }
  table { border-collapse: collapse; width: 100%; margin: 1em 0; }
  th, td { text-align: left; padding: 0.5em 0.7em; border-bottom: 1px solid #ddd; font-size: 0.92em; }
  th { color: #666; font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.04em; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .box { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 1em 1.3em; margin: 1em 0; }
  .narrative { background: #eef3f0; border-left: 3px solid #1f5d50; padding: 0.8em 1em; font-style: italic; }
  .warn { color: #a13c30; font-weight: 600; }
  .badge { display: inline-block; padding: 0.15em 0.55em; border-radius: 4px; font-size: 0.78em; background: #eee; }
  pre { background: #14171a; color: #e9ebe5; padding: 1em; border-radius: 6px; overflow-x: auto; font-size: 0.82em; }
  code { font-family: 'IBM Plex Mono', monospace; }
  nav { font-size: 0.85em; margin-bottom: 1.2em; }
</style>
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><html><head><title>{html.escape(title)}</title>{_STYLE}</head>"
                         f"<body><nav><a href='/demo'>&larr; batch report</a></nav>{body}</body></html>")


def _inr(minor) -> str:
    if minor is None:
        return "0"
    rupees = minor // 100
    s = str(abs(rupees))
    grouped = s if len(s) <= 3 else None
    if grouped is None:
        last3, rest = s[-3:], s[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        grouped = ",".join(parts) + "," + last3
    return "&#8377;" + (("-" if rupees < 0 else "") + grouped)


@router.get("/demo", response_class=HTMLResponse)
def demo_report():
    session = SessionLocal()
    try:
        report = build_report(session, DEMO_BUSINESS_ID)
        text_report = html.escape(render(report))

        latest_batch = session.execute(
            select(RecoveryBatch).where(RecoveryBatch.business_id == DEMO_BUSINESS_ID).order_by(RecoveryBatch.started_at.desc())
        ).scalars().first()
        batch_link = ""
        if latest_batch is not None:
            batch_link = f"<p><a href='/demo/batch/{latest_batch.batch_id}'>Explain this batch &rarr;</a></p>"

        rows = session.execute(
            select(RevenueAtRisk)
            .where(RevenueAtRisk.business_id == DEMO_BUSINESS_ID)
            .order_by(RevenueAtRisk.at_risk_minor.desc())
            .limit(100)
        ).scalars().all()
        table_rows = "\n".join(
            f"<tr><td>{r.entity_type}</td><td>{r.loss_category}</td>"
            f"<td class='num'><a href='/demo/at-risk/{r.at_risk_id}'>{_inr(r.at_risk_minor)}</a></td>"
            f"<td><span class='badge'>{r.status}</span></td><td>{'yes' if r.claimable else 'no'}</td></tr>"
            for r in rows
        )

        body = f"""
        <h1>Batch report</h1>
        <div class="box"><pre>{text_report}</pre></div>
        {batch_link}
        <h2>At-risk records (top 100 by amount)</h2>
        <table>
          <tr><th>Entity</th><th>Category</th><th class='num'>At risk</th><th>Status</th><th>Claimable</th></tr>
          {table_rows}
        </table>
        """
        return _page("Batch report", body)
    finally:
        session.close()


@router.get("/demo/at-risk/{at_risk_id}", response_class=HTMLResponse)
def demo_at_risk(at_risk_id: uuid.UUID):
    session = SessionLocal()
    try:
        at_risk = session.get(RevenueAtRisk, at_risk_id)
        if at_risk is None:
            raise HTTPException(status_code=404, detail="unknown at-risk record")

        explanation = explain(session, "AT_RISK", business_id=DEMO_BUSINESS_ID, at_risk_id=at_risk_id)
        session.commit()

        warn = "" if explanation.numbers_verified else "<p class='warn'>Numbers in this narrative could not be fully verified against the evidence bundle.</p>"

        attempts = session.execute(
            select(RecoveryAttempt).where(RecoveryAttempt.at_risk_id == at_risk_id).order_by(RecoveryAttempt.attempt_number)
        ).scalars().all()
        attempt_rows = "\n".join(
            f"<tr><td>{a.attempt_number}</td><td>{a.strategy}</td><td>{a.channel or '-'}</td>"
            f"<td>{a.cohort}</td><td>{'yes' if a.executed_at else 'no'}</td>"
            f"<td>{a.suppressed_reason or '-'}</td>"
            f"<td><a href='/demo/attempts/{a.attempt_id}'>explain &rarr;</a></td></tr>"
            for a in attempts
        )

        body = f"""
        <h1>{at_risk.entity_type} &middot; {at_risk.loss_category}</h1>
        <div class="box">
          <p><b>At risk:</b> {_inr(at_risk.at_risk_minor)} &middot; <b>Status:</b> <span class="badge">{at_risk.status}</span>
             &middot; <b>Claimable:</b> {'yes' if at_risk.claimable else 'no'} &middot; <b>Fault:</b> {at_risk.fault_attribution}</p>
          <p><b>Detected:</b> {at_risk.detected_at.isoformat()} via <code>{at_risk.detection_rule}</code></p>
        </div>
        <div class="narrative">{html.escape(explanation.narrative)}</div>
        {warn}
        <p><a href='/demo/at-risk/{at_risk_id}/raw'>See the provider's raw bytes &rarr;</a></p>
        <h2>Attempts</h2>
        <table>
          <tr><th>#</th><th>Strategy</th><th>Channel</th><th>Cohort</th><th>Executed</th><th>Suppressed</th><th></th></tr>
          {attempt_rows if attempts else "<tr><td colspan='7'>No attempts yet.</td></tr>"}
        </table>
        """
        return _page(f"{at_risk.entity_type} at risk", body)
    finally:
        session.close()


@router.get("/demo/at-risk/{at_risk_id}/raw", response_class=HTMLResponse)
def demo_raw_bytes(at_risk_id: uuid.UUID):
    session = SessionLocal()
    try:
        at_risk = session.get(RevenueAtRisk, at_risk_id)
        if at_risk is None:
            raise HTTPException(status_code=404, detail="unknown at-risk record")
        raw_event = session.get(RawEvent, at_risk.raw_event_id)
        payload_json = html.escape(json.dumps(raw_event.payload, indent=2)) if raw_event else "(raw event not found)"

        body = f"""
        <h1>Raw provider bytes</h1>
        <p><a href='/demo/at-risk/{at_risk_id}'>&larr; back to at-risk record</a></p>
        <div class="box">
          <p><b>Provider:</b> {raw_event.source_provider if raw_event else '?'}
             &middot; <b>Received:</b> {raw_event.received_at.isoformat() if raw_event else '?'}</p>
        </div>
        <pre>{payload_json}</pre>
        """
        return _page("Raw provider bytes", body)
    finally:
        session.close()


@router.get("/demo/attempts/{attempt_id}", response_class=HTMLResponse)
def demo_attempt(attempt_id: uuid.UUID):
    session = SessionLocal()
    try:
        attempt = session.get(RecoveryAttempt, attempt_id)
        if attempt is None:
            raise HTTPException(status_code=404, detail="unknown attempt")

        scope = "SUPPRESSION" if attempt.suppressed_reason else "ATTEMPT"
        explanation = explain(session, scope, business_id=DEMO_BUSINESS_ID, attempt_id=attempt_id)
        session.commit()

        warn = "" if explanation.numbers_verified else "<p class='warn'>Numbers in this narrative could not be fully verified against the evidence bundle.</p>"

        body = f"""
        <h1>Attempt #{attempt.attempt_number} &middot; {attempt.strategy}</h1>
        <p><a href='/demo/at-risk/{attempt.at_risk_id}'>&larr; back to at-risk record</a></p>
        <div class="box">
          <p><b>Cohort:</b> {attempt.cohort} &middot; <b>Channel:</b> {attempt.channel or '-'}
             &middot; <b>Decided:</b> {attempt.decided_at.isoformat()}</p>
          <p><b>Executed:</b> {'yes at ' + attempt.executed_at.isoformat() if attempt.executed_at else 'no'}
             &middot; <b>Suppressed reason:</b> {attempt.suppressed_reason or '-'}</p>
        </div>
        <div class="narrative">{html.escape(explanation.narrative)}</div>
        {warn}
        """
        return _page(f"Attempt {attempt.attempt_number}", body)
    finally:
        session.close()


@router.get("/demo/batch/{batch_id}", response_class=HTMLResponse)
def demo_batch(batch_id: uuid.UUID):
    session = SessionLocal()
    try:
        batch = session.get(RecoveryBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="unknown batch")

        explanation = explain(session, "BATCH", business_id=DEMO_BUSINESS_ID, batch_id=batch_id)
        session.commit()

        warn = "" if explanation.numbers_verified else "<p class='warn'>Numbers in this narrative could not be fully verified against the evidence bundle.</p>"

        body = f"""
        <h1>Batch {str(batch_id)[:8]}</h1>
        <div class="box">
          <p><b>Scanned:</b> {batch.entities_scanned} &middot; <b>Executed:</b> {batch.actions_executed}
             &middot; <b>Suppressed:</b> {batch.actions_suppressed} &middot; <b>Stopped:</b> {batch.actions_stopped}</p>
          <p><b>At risk:</b> {_inr(batch.at_risk_minor)} &middot; <b>Dry run:</b> {'yes' if batch.dry_run else 'no'}</p>
        </div>
        <div class="narrative">{html.escape(explanation.narrative)}</div>
        {warn}
        """
        return _page("Batch explanation", body)
    finally:
        session.close()
