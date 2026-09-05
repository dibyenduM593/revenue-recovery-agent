import os
import re
import sys

commit = os.environ.get("GIT_COMMIT", "")

msg = sys.stdin.read()

# Universal: strip any Co-Authored-By: Claude/Anthropic trailer line, plus a
# lone blank line left dangling right before it.
msg = re.sub(r"\n*Co-Authored-By:\s*Claude[^\n]*\n?", "\n", msg)
msg = msg.rstrip("\n") + "\n"

REPLACEMENTS = {
    "9b1d57052f7d761c232527fe56f4a417e2314c76": [
        ("Day 13: scoring layer, B2C/B2B enforcement, human review, outbound dispatch queue, live demo",
         "Add scoring layer, B2C/B2B enforcement, human review, outbound dispatch queue, live demo"),
    ],
    "269b557ec5a26fa5cba32438677108147590b2e0": [
        ("Day 3: webhook endpoint, HMAC over raw bytes, verified end to end",
         "Add webhook endpoint, HMAC over raw bytes, verified end to end"),
    ],
    "d454f02db90b7a7c6e6024f507a363057bdbca5d": [
        ("Day 1-2 v2: loss-category taxonomy, revenue_at_risk schema, replace v1 migration",
         "Add loss-category taxonomy, revenue_at_risk schema, replace v1 migration"),
        ("Bridges the codebase to Implementation Plan v2 (revenue_at_risk loss ledger,",
         "Bridges the codebase to the v2 design (revenue_at_risk loss ledger,"),
        ("existed, so Day 1/2 needed rework, not just extension.",
         "existed, so the original schema needed rework, not just extension."),
    ],
    "9c7921e02ad7d1bc64d7c73de3d4181bd7dfe943": [
        ("Day 4: worker + normalization stages 1-3",
         "Add worker + normalization stages 1-3"),
        ("a real gap in the Day 2 schema, caught here.",
         "a real gap in the original schema, caught here."),
    ],
    "f8b398b164ff981b2a452092a106cf0c7999c411": [
        ("Day 3: CSV/JSON import endpoint, verified end to end against real Postgres",
         "Add CSV/JSON import endpoint, verified end to end against real Postgres"),
        ("matching the Day 3 done-when criteria from the plan.",
         "matching the done-when criteria for this stage."),
    ],
    "94a2eec24fee0c40aa219d6c555eb03ca08aba9f": [
        ("Day 3: synthetic provider-shaped event generator",
         "Add synthetic provider-shaped event generator"),
    ],
    "d896d601087b9411e65c6b11a878f135b07d7874": [
        ("Day 2: full schema (16 tables) and initial migration",
         "Add full schema (16 tables) and initial migration"),
    ],
    "5a9f427f457ee4edb722e3a804367a7cfca7bd81": [
        ("Day 1: repo scaffold, failure taxonomy, Money type, FastAPI + Alembic skeleton",
         "Add repo scaffold, failure taxonomy, Money type, FastAPI + Alembic skeleton"),
    ],
}

for old, new in REPLACEMENTS.get(commit, []):
    msg = msg.replace(old, new)

sys.stdout.write(msg)
