# score_ml@v1 -- corpus training run

The run that produced `b2c.json` / `b2b.json` in this directory. Kept as
the record of scale; the exported training parquet it consumed
(`data/b2c.parquet`, `data/b2b.parquet`, ~2.9MB) is deliberately NOT in
the repo -- it is regenerable from the ledger with
`python -m app.scoring.export --out data`, and the weights are what
actually matter for results.

Business: `4a8595c7-6022-5acf-93bc-1199bb6fd212` (corpus seed 1003, a
separate business id from the demo's own seed-42 one, so a corpus run
never contaminates the demo ledger).

## Bounds raised for the corpus run

The demo's defaults are deliberately small; a corpus run needs the whole
population in one batch, not 500 of it.

| bound | demo | corpus |
|---|---|---|
| `max_entities_per_batch` | 500 | 45,000 |
| `human_approval_above_minor` | 50,00,000 | 2,50,00,000 |

## Ingestion

```
imports:               231,756 events received
webhook smoke test:    7 posted
worker drain:          179,251 processed, 7,285 requeued, 1,822 dead-lettered
customer_contactability: 48,000 rows
reference seed:        62 field_mappings, 28 value_mappings,
                       9 failure_taxonomy, 12 message_templates
```

## Risk detection (sweeps)

```
abandoned checkouts:  4,905 newly at risk
overdue invoices:     21,112 newly at risk
```

## Decisions

```
iteration 1:  39,736 scanned -> 17,178 executed, 1,492 suppressed, 21,066 stopped
              (Rs 24,52,17,635 at risk scanned)
iteration 2:  21,066 scanned -> 0 executed, 0 suppressed, 21,066 stopped
              (Rs 6,35,27,629 -- the holdout, correctly re-stopped rather than
               re-decided, which is the cohort stability assign_cohort() promises)
```

## Simulated outcomes

```
considered:  38,244
boosted:     17,178   (treatment, actually contacted)
base_only:   21,066   (holdout / suppressed -- organic recovery only)
recovered:   7,424
```

## Attribution

```
strong:      3,292
weak:        4,148
expired:     30,804
still_open:  0
```

## Reproducing

```bash
python -m app.scoring.export --out data        # rebuild the parquet from the ledger
python -m app.scoring.train --data-dir data    # retrain, overwrites b2c.json / b2b.json
```

`train.py` prints holdout AUC, the learning curve and feature importances
to stdout -- it does not persist them, so capture that output if you need
the metrics alongside the weights.
