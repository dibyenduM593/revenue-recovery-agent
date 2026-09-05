"""Gate D: `make demo` (or `python scripts/demo.py` directly --

no `make` required, since Windows dev boxes often don't have it and this
build should run cleanly for whoever pulls it). One command chaining
every stage that already works standalone:

  drop -> migrate -> reference seed -> generate (800 customers, real HTTP)
  -> sweeps -> decide+bound+execute -> simulate outcomes (real ingestion)
  -> attribute -> render the batch report

Deterministic by construction: every stage is seeded (--seed 42
throughout) and the generator's base timestamp is fixed, never
datetime.now() for anything that affects the numbers. Pass
--verify-determinism to run the whole chain twice from an empty database
and diff the two batch reports byte-for-byte -- the actual Gate D
acceptance check, not just a claim.

Each stage runs as its own subprocess (python -m <module>), the same way
a human would type these commands one at a time -- process isolation
between stages, no shared import-time state to worry about, and it fails
loudly (non-zero exit, stderr shown) the moment any single stage does.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # so `python scripts/demo.py` finds app.settings regardless of cwd

sys.stdout.reconfigure(encoding="utf-8")  # the batch report's rupee sign isn't in Windows' default cp1252 console

PYTHON = sys.executable


def _run(step: str, module_args: list[str]) -> float:
    start = time.monotonic()
    print(f"\n=== {step} ===", flush=True)
    result = subprocess.run(
        [PYTHON, "-m", *module_args], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    elapsed = time.monotonic() - start
    sys.stdout.write(result.stdout)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(f"step {step!r} failed (exit {result.returncode}) after {elapsed:.1f}s")
    print(f"--- {step} done in {elapsed:.1f}s ---", flush=True)
    return elapsed


def drop_database() -> None:
    from sqlalchemy import create_engine, text

    from app.settings import DATABASE_URL

    engine = create_engine(DATABASE_URL, future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def run_once(seed: int, customers: int) -> tuple[str, dict]:
    """Runs the full chain from an empty database. Returns (report_text, timings)."""
    timings: dict[str, float] = {}
    total_start = time.monotonic()

    print("\n=== drop database ===", flush=True)
    t0 = time.monotonic()
    drop_database()
    timings["drop"] = time.monotonic() - t0
    print(f"--- drop database done in {timings['drop']:.1f}s ---", flush=True)

    timings["migrate"] = _run("migrate", ["alembic", "upgrade", "head"])
    timings["reference seed"] = _run("reference seed", ["seed.reference", "--seed", str(seed)])
    timings["generate"] = _run(
        "generate", ["seed.generate", "--seed", str(seed), "--customers", str(customers)]
    )
    timings["sweeps"] = _run("sweeps", ["app.risk.sweeps"])
    timings["decide + bound + execute"] = _run(
        "decide + bound + execute", ["app.recovery.run", "--seed", str(seed), "--live"]
    )
    timings["simulate outcomes"] = _run("simulate outcomes", ["seed.simulate_world", "--seed", str(seed)])
    timings["attribution"] = _run("attribution", ["app.recovery.attribution", "--seed", str(seed)])

    report_start = time.monotonic()
    result = subprocess.run(
        [PYTHON, "-m", "app.recovery.report", "--seed", str(seed)],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    timings["report"] = time.monotonic() - report_start
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit("report step failed")

    timings["total"] = time.monotonic() - total_start
    return result.stdout.strip(), timings


def _print_timings(timings: dict) -> None:
    for step, seconds in timings.items():
        marker = "  <- total" if step == "total" else ""
        print(f"  {step:>26}: {seconds:7.1f}s{marker}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gate D demo: the full pipeline, one command, no make required.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--customers", type=int, default=800)
    parser.add_argument(
        "--verify-determinism", action="store_true",
        help="run the full chain twice from empty and confirm the batch report matches byte-for-byte",
    )
    args = parser.parse_args()

    print("=" * 72)
    print("AI Revenue Recovery -- Gate D demo run")
    print(f"seed={args.seed}  customers={args.customers}")
    print("=" * 72)

    report_1, timings_1 = run_once(args.seed, args.customers)
    print("\n" + "=" * 72)
    print("BATCH REPORT")
    print("=" * 72)
    print(report_1)
    print("\nTIMINGS (run 1)")
    _print_timings(timings_1)

    if not args.verify_determinism:
        print(f"\nDone in {timings_1['total']:.1f}s.")
        print("Pass --verify-determinism to run the whole chain twice and confirm identical numbers.")
        return

    print("\n" + "=" * 72)
    print("Running the whole chain a second time from empty, to verify determinism...")
    print("=" * 72)
    report_2, timings_2 = run_once(args.seed, args.customers)
    print("\nTIMINGS (run 2)")
    _print_timings(timings_2)

    print("\n" + "=" * 72)
    if report_1 == report_2:
        print("DETERMINISM CHECK: PASS -- both runs produced an identical batch report.")
    else:
        print("DETERMINISM CHECK: FAIL -- the two runs produced different numbers.")
        print("--- run 1 ---\n" + report_1)
        print("--- run 2 ---\n" + report_2)
        raise SystemExit(1)
    print("=" * 72)
    print(f"Total time for both runs: {timings_1['total'] + timings_2['total']:.1f}s")


if __name__ == "__main__":
    main()
