# Internal Revenue Loss: Where It Hides, What We Can Reach, and What We Can Actually Recover

Scheduled research run — 2026-08-30. Written against the existing `app/` build
(canonical events → normalize → risk → policy → bounds → channels → attribution).

---

## 0. The frame

"How a CA finds loopholes" is the right instinct, and it's worth being precise about
what makes that job work. A CA doesn't find money by being smarter about the business.
They find it by **reconciling two records that were never designed to agree** — books vs.
bank, purchase register vs. GSTR-2B, invoice vs. contract, charge vs. rate card — and
then acting on the delta before a deadline expires.

That is the entire product. Every category below is a **two-sided reconciliation with a
clock on it**. Our advantage over a human CA is not judgment; it's that we can run the
reconciliation continuously at line-item level and file the claim before the window closes.

The critical distinction that shapes everything downstream:

| | Lost revenue (external) | Leaked revenue (internal) |
|---|---|---|
| Cause | Customer didn't want to buy | Money was earned but never landed |
| Evidence | Doesn't exist | **Exists, in two systems that disagree** |
| Recovery | Sell harder | Re-attempt, chase, or file a claim |
| Our fit | Poor | **This is the whole opportunity** |

We only build for column two.

---

## 1. Taxonomy of internal revenue loss

Four families. Within each, the leak, how it happens, and rough scale.

### Family A — Collection failure (money owed, never collected)

The customer agreed to pay. The payment didn't complete.

| # | Leak | Mechanism | Scale (indicative) |
|---|---|---|---|
| A1 | Failed recurring payments / involuntary churn | Card expired, insufficient funds, issuer soft-decline, bank downtime. Retry never happens or happens at the wrong hour. | 10–15% of recurring charges fail on first attempt; involuntary churn is 20–40% of *total* churn |
| A2 | Checkout abandonment at the payment step | User reached payment, dropped at OTP/redirect/bank page. Distinct from cart abandonment — intent was proven. | High-intent slice of a much larger abandonment pool |
| A3 | Overdue invoices / no dunning ladder | B2B invoices past due with no systematic chase. | Billing errors + collections are cited as the single largest leakage contributor (~38% of leakage) |
| A4 | Revoked/expired mandates (UPI Autopay, e-NACH, SI) | Mandate dies silently; the next charge can't even be attempted. | Structural in India; a charge is impossible, not just failed |

**Recoverability: very high.** Dunning median recovers ~half of failed charges; well-run
programmes are cited at 50–85%. This is the best-understood family and the one the
current build already covers.

### Family B — Silent overcharge by a counterparty (money paid out that shouldn't have been)

Someone else's system billed us wrong. Nobody checked line by line.

| # | Leak | Mechanism | Scale (indicative) |
|---|---|---|---|
| B1 | Courier weight discrepancy | Declared 1.0 kg, billed at 1.8 kg on volumetric rules. Appears as ₹10–50 per shipment on an invoice weeks later, deducted from wallet. | Estimated ₹3,500 cr/yr industry-wide in Indian ecommerce shipping inefficiency, weight discrepancy the largest slice |
| B2 | Courier zone / COD / RTO mis-rating | Wrong zone applied, COD fee charged on prepaid, RTO billed at forward rate. | Rides alongside B1 in the same invoice |
| B3 | Marketplace fee and commission errors | Referral fee at wrong category rate, closing fee on a cancelled order, shipping fee charged twice. | Bundled into "1–3% of Amazon revenue lost to unflagged FBA/fee errors" |
| B4 | FBA/marketplace lost & damaged inventory | Units lost inbound or in-warehouse, damaged, disposed without reimbursement, customer refund issued but unit never returned. | 1–3% of annual marketplace revenue |
| B5 | Payment gateway fee / settlement shortfall | MDR applied above contracted rate, GST on fee miscomputed, a settlement batch short of the sum of its payments, refund fees not reversed. | Small % but on 100% of GMV — compounds |
| B6 | Duplicate & erroneous vendor payments (AP) | Same invoice paid twice via different entry paths; credit notes never applied. | Benchmarks range 0.1–2% of AP spend; first-time recovery audits recover 0.05–0.5% of audited spend (~$1M per $1B) |

**Recoverability: high but deadline-bound.** Courier dispute windows are brutally short —
roughly 7–15 days depending on carrier. Marketplace reimbursement windows are policy-set
and have been tightening. **A leak found on day 20 is worth zero.** This family is where
continuous automation beats a quarterly human audit by the widest margin.

### Family C — Price, contract and configuration drift (money never billed at all)

The invoice was correct given the config. The config was wrong.

| # | Leak | Mechanism |
|---|---|---|
| C1 | Unbilled usage | Metered usage recorded but not rated onto an invoice; events dropped between product and billing. |
| C2 | Stale rate cards | Contract says ₹X, billing system says ₹X-minus-old-discount. Nobody re-syncs after renewal. |
| C3 | Discounts and promos that never expired | 3-month intro price still applied in month 30. |
| C4 | Missed contractual escalators | Annual 5% uplift clause never executed. |
| C5 | Entitlement leakage | Trial ended, access didn't. Seat count in product exceeds seat count on invoice. |
| C6 | Under-recovered pass-throughs | Shipping/handling/surcharges absorbed instead of billed. |

**Recoverability: highest margin, hardest access.** This is the purest "CA finds a loophole"
category and it is largely invisible to payment infrastructure — it needs the contract,
the product's usage log, and the invoice in one place. It's also the least contested
recovery: you're billing your own customer correctly going forward, no counterparty to fight.
Forward-looking correction is easy; back-billing is a commercial decision, not a technical one.

### Family D — Tax, credit and dispute abandonment

| # | Leak | Mechanism | Scale |
|---|---|---|---|
| D1 | GST ITC lost to GSTR-2B mismatch | Under §16(2)(aa) CGST, ITC is claimable **only** if the invoice appears in your GSTR-2B. Supplier didn't upload, uploaded wrong GSTIN/number/amount, or filed late → your credit vanishes. | Directly cash — every ₹100 of lost ITC is ₹100 of extra tax paid |
| D2 | TDS/TCS credit mismatch (26AS/AIS vs. books) | Deductor filed wrong PAN/amount; credit not reflected. | Same mechanic, same cash effect |
| D3 | Chargebacks never fought | Merchants accept ~46% of chargebacks outright. Representment win rate averages ~54% among *fought* disputes; overall chargeback-level win rate ~11%. | The gap between 54% and 11% is the opportunity |
| D4 | Refund and cancellation errors | Refunded twice, refunded more than charged, refunded an order that also charged back (double dip). | Small count, high per-item value |

**Recoverability on D1/D2: extremely high and legally clean** — it's not a negotiation,
it's a reconciliation plus a vendor chase. On D3: mechanical, evidence-driven, and mostly
unfought purely because nobody had time.

---

## 2. Which of these are worth building

Score = Impact × Detectability × Recoverability × Data access. Detectability is where
most "AI for finance" ideas die: if the rule can't be written deterministically, the
finding can't be trusted enough to act on.

| Leak | Impact | Deterministic? | Recovery is | Data access | Verdict |
|---|---|---|---|---|---|
| A1 failed recurring | High | Yes | Re-attempt + nudge | Webhook, real-time | **Build — done** |
| A2 checkout abandonment | Medium | Yes (sweep) | Nudge | Webhook/API | **Build — done** |
| A3 overdue invoices | High | Yes (sweep) | Nudge ladder | API | **Build — done** |
| A4 mandate revoked | High | Yes | Re-collect mandate | Webhook | **Build — done** |
| B1/B2 courier overcharge | High | **Yes — arithmetic** | File claim, 7–15 day window | File/CSV mostly | **Build next — highest ROI** |
| B4 FBA lost/damaged | High | Yes — inventory ledger | File claim | SP-API reports (async) | **Build next** |
| B3 marketplace fee errors | Medium | Yes — fee schedule math | File claim | Settlement reports | Build after B4 |
| B5 gateway fee/settlement | Low-Med | Yes — recompute MDR | Support ticket | Recon API | Cheap add-on, strong trust signal |
| B6 duplicate AP payments | High ($) | Yes — fuzzy match | Vendor credit note | ERP export/file | Build if we go mid-market |
| D1 GST ITC mismatch | Very high | Yes — invoice match | Vendor chase | **GSP-gated** | High value, real access friction |
| D3 chargebacks unfought | Medium | Partly | Representment | Webhook + API | Good LLM fit (evidence assembly) |
| C1–C6 contract drift | Very high | **Only with contract data** | Re-bill | Contracts are PDFs | Highest ceiling, latest phase |

**The shortlist:** stay on Family A (built), add **B1/B2 courier reconciliation** and
**B4 marketplace reimbursement** next. Both are pure arithmetic against a rate card or an
inventory ledger — zero model risk, and the deadline pressure means the customer genuinely
cannot do it manually. D1 (GST) is the biggest single number but carries an access
dependency we don't control. C is the long game.

---

## 3. Can we actually get the data? (The honest answer: it's four tiers, and files never go away)

This is the question that decides the architecture, so it deserves a blunt answer.

### Tier 1 — Real-time push. Webhook + REST API, signed, idempotent.
Razorpay, Stripe, Cashfree, PayU, Shopify, subscription billers.
- Razorpay: webhooks for orders, payments, settlements, disputes, refunds, subscriptions;
  plus `GET /v1/settlements/recon/combined` returning every payment, refund, transfer and
  **adjustment** settled on a given day/month, with fees, tax and UTR. That adjustment line
  is exactly what B5 needs.
- This tier is a solved problem. HMAC-signed, replayable, freshness-windowed.

### Tier 2 — Pull-only, async, batch. API exists but behaves like a file service.
Amazon SP-API, marketplace settlement reports, Zoho Books / QuickBooks, courier aggregator APIs.
- SP-API's FBA reports are **request → poll → download a flat file**, with documented
  guidance not to request reports more often than they regenerate. It is an API wrapping a
  file. Report types get deprecated and removed on published dates (several FBA report
  types were retired through 2025), so connectors need version tolerance.
- Practically: this tier is a scheduled fetch, not a stream. Latency is hours, not seconds.

### Tier 3 — Regulated intermediary. You cannot connect directly.
- **GSTN**: no direct API access. You go through a **GSP**, and GSTN has stopped accepting
  new GSP registrations — the only route now is to become an **ASP** riding on an existing
  GSP's tunnel. Auth is OTP-driven with sessions from 6 hours to 30 days. This is a
  commercial partnership and a compliance posture, not an integration ticket.
- **Bank data**: Account Aggregator framework, or statement upload. Same shape.

### Tier 4 — Files. Genuinely files.
Courier invoices (PDF/XLS, arriving by email weeks after the shipment), Tally (no cloud
API — local XML/ODBC), ERP exports, remittance advices, older marketplace panels.
- **The single most valuable leak we identified (B1 courier overcharge) lives almost
  entirely in Tier 4.** The rate card is a PDF or a negotiated sheet. The invoice is a
  spreadsheet. The manifest is an export.

### What this means architecturally

The answer to "webhooks and APIs for everything, or file uploads?" is **both, permanently,
and the ones that pay best are files.** Any design that assumes clean webhooks will be
unable to touch Families B and C — which is where the differentiated money is.

**We already got this right.** `POST /v1/webhooks/{provider}` and `POST /v1/imports` both
land in the same idempotent `raw_events` insert, and normalization runs identically
downstream. That is not a convenience — it's the thing that lets a courier invoice PDF and
a Razorpay webhook produce the same canonical loss event. Every new source is then a
`field_mappings`/`value_mappings` entry plus a fetch schedule, not new architecture.

The one gap: Tier 2 and Tier 4 need a **scheduled fetcher + inbound-email intake** that
doesn't exist yet. That's the next infrastructure piece — small, and it unlocks the whole
of Family B.

---

## 4. How the money actually comes back

Three recovery modes. They have completely different risk profiles and should not share code paths.

**Mode 1 — Re-attempt.** Retry the charge at a better time, on a better rail.
Fully automatable, no counterparty, no consent issue. Bounded by network retry rules.
*Applies to: A1, A4.*

**Mode 2 — Persuade.** Nudge the customer: dunning email, cart recovery link, mandate
re-collection, invoice chase. Automatable but **consent-bound** — DPDP Act, TRAI DND,
quiet hours, contact frequency caps, hard bounces. Every suppression must be recorded,
not silently dropped. *Applies to: A1–A4.* Already implemented via `bounds.py`'s H1–H10.

**Mode 3 — Claim.** File against a counterparty with evidence, before a deadline:
- Courier weight dispute → portal/API claim with weight+dimension proof, 7–15 days
- Marketplace reimbursement → case with inventory ledger reconciliation
- Chargeback representment → evidence pack to the acquirer
- Vendor credit note → duplicate-payment recovery letter
- GST ITC → chase the supplier to correct their GSTR-1

This is the CA-shaped work, it's the highest margin, and **it's the half we haven't built.**
Mode 3 differs from Modes 1–2 in three ways that matter:
1. **It has a hard expiry.** Detection latency directly destroys value.
2. **It requires an evidence bundle**, not just a decision. Photos, weights, timestamps, ledger deltas.
3. **A wrong claim has a reputational cost** with the counterparty — false-positive tolerance
   is far lower than for a dunning email.

Commercially, Mode 3 also supports success-fee pricing (third-party FBA reimbursement
services charge 10–20% of recovered amount), which is a much easier sell than SaaS
subscription for an unproven category.

---

## 5. What the actors look like

Design principle, stated once because everything follows from it:

> **Deterministic code decides what money is owed. The model only explains, extracts, and
> assembles evidence.** No LLM ever computes an amount, and no LLM ever authorizes a send.

Every actor below is a bounded worker with typed input/output, an idempotency key, a
version stamp, and a defined failure mode. They compose into the pipeline we already have.

### Ingest layer

**`connector.<source>`** — one per source, three flavours sharing an interface:
- *webhook* (Tier 1): verify HMAC over raw bytes, freshness window, insert to `raw_events`
- *poller* (Tier 2/3): scheduled fetch, cursor/watermark state, backoff, report-type version tolerance
- *intake* (Tier 4): file/email → attachment → `POST /v1/imports`

Output: `raw_events` rows. Failure mode: dead-letter with the raw payload retained, never
a silent drop. *Status: webhook + import exist; poller and email intake don't.*

**`extractor.document`** — the one place a model is allowed near ingestion. PDF/scan →
structured line items (courier invoice, rate card, vendor invoice, contract clause).
Every extracted field carries a confidence and a page/bbox citation. Below threshold →
human review queue. **Extraction output is treated as a claim, never as a fact, until it
reconciles against a second source.**

### Normalize layer

**`normalizer`** — the existing six stages (structural → typing → units → semantic →
validation → upsert). Unrecognized values dead-letter rather than pass through. This is
already the right discipline and it extends unchanged to Family B sources.

### Detect layer — the "CA" actors

One actor per leak, each a deterministic rule with an explicit version. Two shapes:

- **Event-driven** (`detect.payment_failed`, `detect.mandate_revoked`) — a direct loss
  signal arrives, open an at-risk record. *Exists.*
- **Sweep-driven** (`sweep.checkout_abandoned`, `sweep.invoice_overdue`) — scan current
  state on a schedule. *Exists.*
- **Reconciliation-driven** (new, and the whole of Family B/C/D) — join two independently
  sourced records and emit the delta:

| Actor | Left record | Right record | Delta = |
|---|---|---|---|
| `recon.courier_weight` | Manifest declared weight/dims | Courier invoice billed weight | Overcharge + dispute deadline |
| `recon.courier_rate` | Contracted rate card | Invoice line rate | Zone/COD/RTO mis-rating |
| `recon.fba_inventory` | Inbound shipped + returns | Amazon inventory ledger | Missing units, unreimbursed |
| `recon.marketplace_fee` | Published fee schedule | Settlement report line | Fee overcharge |
| `recon.settlement` | Sum of payments/refunds | Settlement recon API total | Shortfall / unexplained adjustment |
| `recon.ap_duplicate` | Invoice register | Payment register | Same invoice, two payments |
| `recon.gst_itc` | Purchase register | GSTR-2B | Unmatched / mismatched ITC |
| `recon.contract_price` | Contract terms | Invoice line | Stale rate, expired discount, missed escalator |

All of these funnel into the **same `revenue_at_risk` ledger** the build already has, with
`loss_category`, `fault_attribution`, `claimable`, and a `detection_rule`/`detection_version`
pair. The existing partial-unique-index idempotency (one open record per entity) generalises
cleanly. Add one field: **`claim_deadline_at`** — Family B is worthless without it.

**`evidence.assembler`** (new) — for every claimable finding, build the proof pack the
counterparty requires: the two source documents, the computed delta with its arithmetic
shown, timestamps, lineage back to `raw_event_id`. This is where an LLM adds real value —
composing a persuasive, format-correct claim narrative — over numbers it did not invent.

### Decide layer

**`policy.decide`** — pure, side-effect-free, versioned. Extends naturally from
`(failure_reason, attempt_number, entity_type, mandate_state, value_minor)` to a claim
variant: `(leak_type, delta_minor, evidence_strength, days_to_deadline) → ClaimAction`.
*Exists for Family A.*

**`bounds.execute_action`** — the single chokepoint. Every dispatch passes through it or
doesn't happen. For claims it needs new bounds beyond H1–H10:
- minimum claim value (don't burn counterparty goodwill on ₹12)
- per-counterparty claim rate cap (don't look like an abuser)
- evidence-completeness gate (never file an unsupported claim)
- deadline check (never file into a closed window — mark expired and report it)
- human approval above a value threshold *(already the pattern for Family A)*

### Act layer

**`channel.*`** — customer-facing sends (email real, others simulated and labelled as such).
*Exists.*
**`filer.<counterparty>`** (new) — submits the claim: courier portal/API, marketplace case,
acquirer representment, vendor credit-note request. Then **tracks its state machine**:
filed → acknowledged → approved/rejected → credited. A claim that's filed and forgotten is
the same as no claim.

### Measure layer

**`attribution`** — matches recoveries back to attempts inside each attempt's own decided-at
window, against a deterministic-hash holdout cohort. *Exists — and this is the part most
competitors don't have.* For claims, attribution is simpler and stronger: the credit note
or reimbursement is directly traceable, so "recovered" is a fact rather than an estimate.

**`explainer`** (new, Day 11 in the plan) — natural-language "here is what we found, here
is the arithmetic, here is the evidence" over an already-computed finding. Read-only over
verified numbers.

### The shape of it

```
sources ──► connector.* ──► raw_events ──► normalizer ──► canonical tables
                 ▲                                              │
        extractor.document                                      ▼
        (files/PDFs, cited)                    detect.* / sweep.* / recon.*
                                                                │
                                                                ▼
                                                        revenue_at_risk
                                                     (+ claim_deadline_at)
                                                                │
                                              evidence.assembler│
                                                                ▼
                                                        policy.decide
                                                                │
                                                    bounds.execute_action   ◄── the only exit
                                                            ╱        ╲
                                                   channel.*          filer.*
                                                   (persuade)         (claim)
                                                            ╲        ╱
                                                          attribution
                                                                │
                                                        report / explainer
```

The important property: **there is exactly one path from a detected loss to a real-world
action, and it runs through bounds.** That's what makes an autonomous money-touching agent
defensible.

---

## 6. Recommended sequence

1. **Add `claim_deadline_at` to `revenue_at_risk`** and a deadline sweep. One column,
   unlocks the entire claim half. Cheap, do it first.
2. **Build `connector.poller` + email intake.** Small piece of infrastructure; without it
   Tier 2/4 sources are unreachable and Family B is off the table.
3. **Ship `recon.courier_weight` end to end** — extract → reconcile → evidence → file →
   track → attribute. It's the cleanest proof of the whole thesis: pure arithmetic, a hard
   deadline no human can reliably meet, and a customer who feels the pain monthly.
4. **`recon.fba_inventory`**, reusing the same claim machinery.
5. **`recon.settlement` / `recon.marketplace_fee`** — cheap given (3) and (4), and being
   able to say "your gateway overcharged you ₹X" builds enormous trust fast.
6. **GST ITC** only once a GSP/ASP relationship is real. Biggest number, longest lead time.
7. **Contract drift (Family C)** last — highest ceiling, needs contract ingestion maturity
   that (2) and `extractor.document` will have built by then.

---

## 7. Caveats worth stating plainly

- **The published statistics are soft.** Most revenue-leakage percentages circulating
  online come from vendors selling leakage software, and a growing share of the top search
  results are SEO/AI-generated aggregator sites citing each other. Ranges here (1–5% of
  revenue, 9% of MRR to failed payments, 1–3% of marketplace revenue) are directionally
  useful for prioritisation and **not usable in a customer-facing claim**. Any number we
  quote to a customer should come from their own reconciled data — which is, conveniently,
  exactly what the product produces.
- **Amazon changed the reimbursement basis in March 2025** from selling price to
  sourcing/manufacturing cost, and moved toward auto-reimbursing some lost/damaged cases.
  Both shrink the addressable pool and mean the FBA connector must know which cases are
  still manually claimable. Verify current policy before building step (4).
- **Claim windows are the binding constraint**, not detection accuracy. A 7-day courier
  window means the pipeline's SLA is measured in hours.
- **False-positive cost is asymmetric in Mode 3.** A wrong dunning email costs a little
  goodwill; a pattern of wrong claims gets an account flagged. The evidence-completeness
  bound is not optional.
- **Consent law is live and enforced** (DPDP Act, TRAI DND). The existing distinction —
  permanent compliance facts close a loss, temporary conditions defer it — is correct and
  should not be relaxed under pressure to show recovery numbers.
- **GSP/ASP dependency is a business risk, not a technical one.** Don't put GST on a
  critical path with a fixed date.

---

## Sources

- [Revenue Leakage in SaaS: You're Losing 1-5% of Revenue — LedgerUp](https://www.ledgerup.ai/revenue-leakage)
- [Revenue Leakage Explained and Why B2B Companies Should Care — Enable](https://www.enable.com/resources/articles/revenue-leakage-explained-and-why-b2b-companies-should-care/)
- [Involuntary Churn: How to Reduce Payment-Failure Churn — Baremetrics](https://baremetrics.com/blog/involuntary-churn)
- [Failed-Payment Recovery: The 2026 Dunning Playbook — Digital Applied](https://www.digitalapplied.com/blog/failed-payment-recovery-dunning-playbook-2026)
- [Fetch Settlement Recon Details — Razorpay Docs](https://razorpay.com/docs/api/settlements/fetch-recon/)
- [Settlements Webhook Events — Razorpay Docs](https://razorpay.com/docs/webhooks/settlements/)
- [All You Need to Know About GST API Access — ClearTax](https://cleartax.in/s/gst-api-access)
- [ITC Mismatch in GST — How to Fix It Permanently](https://www.erpgroup.in/blog/itc-mismatch-in-gst-how-to-fix-it-permanently)
- [ITC Reconciliation Errors: How Businesses Can Avoid Them — Tally](https://tallysolutions.com/business-guides/itc-reconciliation-common-mistakes-gst/)
- [Fulfillment by Amazon (FBA) Reports — Amazon SP-API docs](https://developer-docs.amazon.com/sp-api/docs/report-type-values-fba)
- [Deprecation Reminders, November 2025 — Amazon SP-API](https://developer-docs.amazon/sp-api/changelog/deprecation-reminders-november-2025)
- [Key Amazon Reimbursement Policy Details and Updates — Carbon6](https://www.carbon6.io/blog/amazon-reimbursement-policy/)
- [Weight Discrepancy in Ecommerce Shipping India — TrackVid](https://trackvid.in/blogs/weight-discrepancy-ecommerce-shipping-india.html)
- [How to Win Courier Weight Discrepancy Disputes in 2026 — Metaport](https://metaport.in/courier-weight-discrepancy-dispute/)
- [Shiprocket and Delhivery: Shipping Reconciliation Guide — Busy](https://busy.in/ecommerce-reconciliation/shiprocket--delhivery-shipping-reconciliation-match-invoices--catch-overcharges/)
- [What Percentage of AP Spend Is Lost to Duplicate Payments — Transparent](https://transparentglobal.com/blog/what-percentage-of-ap-spend-is-lost-to-duplicate-payments-industry-benchmarks/)
- [Accounts Payable Recovery Audit Guide — PRGX](https://www.prgx.com/guides/ap-recovery-audit-services-guide/)
- [How Often Do Merchants Win Chargeback Disputes? — Chargeback Gurus](https://www.chargebackgurus.com/blog/how-often-do-merchants-win-chargeback-disputes)
- [Chargeback Win Rate — Solidgate](https://solidgate.com/glossary/chargeback-win-rate/)
