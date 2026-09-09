import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _run(step: str, module_args: list[str]) -> str:
    t0 = time.monotonic()
    print(f"=== {step} ===", flush=True)
    result = subprocess.run([PY, "-m", *module_args], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8")
    elapsed = time.monotonic() - t0
    sys.stdout.write(result.stdout)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(f"step {step!r} failed (exit {result.returncode})")
    print(f"--- {step} done in {elapsed:.1f}s ---", flush=True)
    return result.stdout


def _actions_executed(stdout: str) -> int:
    for line in stdout.splitlines():
        if "actions_executed:" in line:
            return int(line.split(":")[1].strip())
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--customers", type=int, required=True)
    parser.add_argument("--checkouts", type=int, required=True)
    parser.add_argument("--invoices", type=int, required=True)
    parser.add_argument("--subscriptions", type=int, required=True)
    parser.add_argument("--invoice-overdue-rate", type=float, default=0.35)
    parser.add_argument("--max-iterations", type=int, default=60)
    parser.add_argument("--anchor", type=str, default=None, help="ISO8601 date the generated window ends at")
    parser.add_argument("--no-relax-bounds", dest="relax_bounds", action="store_false", default=True)
    parser.add_argument("--now", type=str, default=None, help="ISO8601 batch decision time")
    parser.add_argument(
        "--clock", type=str, default=None,
        help="ISO8601 moment attribution evaluates windows as of. Without this, long-window items "
             "(90-day invoices) never expire and only their RECOVERED cases reach the corpus.",
    )
    args = parser.parse_args()

    total_start = time.monotonic()
    seed = str(args.seed)

    _run("reference seed", ["seed.reference", "--seed", seed])

    # The demo's own bounds (500 per batch, Rs 50k approval gate) stall a
    # corpus-scale pool: score x amount floats the biggest items to the top
    # of every batch, they all trip the approval gate, and the run executes
    # nothing. Relaxed for this disposable business only -- never seed 42.
    if args.relax_bounds:
        subprocess.run(
            [PY, str(REPO_ROOT / "scripts" / "set_training_bounds.py"), "--seed", seed],
            cwd=REPO_ROOT, check=True,
        )

    generate_args = [
        "seed.generate", "--seed", seed, "--customers", str(args.customers),
        "--checkouts", str(args.checkouts), "--invoices", str(args.invoices),
        "--subscriptions", str(args.subscriptions),
        "--invoice-overdue-rate", str(args.invoice_overdue_rate),
    ]
    if args.anchor:
        generate_args += ["--anchor", args.anchor]
    _run("generate", generate_args)
    generate_done = time.monotonic()
    print(f"[elapsed since start: {generate_done - total_start:.1f}s]", flush=True)

    _run("sweeps", ["app.risk.sweeps"])

    print("=== decide + bound + execute (looped until drained) ===", flush=True)
    iterations = 0
    total_executed = 0
    while iterations < args.max_iterations:
        iterations += 1
        run_args = ["app.recovery.run", "--seed", seed, "--live"]
        if args.now:
            run_args += ["--now", args.now]
        stdout = _run(f"decide+execute iteration {iterations}", run_args)
        executed = _actions_executed(stdout)
        total_executed += executed
        print(f"  iteration {iterations}: executed={executed} (cumulative={total_executed})", flush=True)
        if executed == 0:
            print("  no actions executed this iteration -- remaining pool is holdout/approval-held. stopping.", flush=True)
            break
    batch_loop_done = time.monotonic()
    print(f"[decide+execute loop: {iterations} iterations, {total_executed} total executed, "
          f"{batch_loop_done - generate_done:.1f}s]", flush=True)

    _run("simulate outcomes", ["seed.simulate_world", "--seed", seed])

    attribution_args = ["app.recovery.attribution", "--seed", seed]
    if args.clock:
        attribution_args += ["--clock", args.clock]
    _run("attribution", attribution_args)

    export_stdout = _run("export", ["app.scoring.export", "--seed", seed, "--out", "data/"])

    total_elapsed = time.monotonic() - total_start
    print(f"\n=== TRAINING CORPUS RUN TOTAL: {total_elapsed:.1f}s ({total_elapsed / 60:.1f} min) ===")
    print(export_stdout)


if __name__ == "__main__":
    main()
