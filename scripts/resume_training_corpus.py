
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
    parser.add_argument("--max-iterations", type=int, default=100)
    parser.add_argument(
        "--now", type=str, default=None,
        help="Backdated ISO8601 decision time, passed through to app.recovery.run --now. Required in "
             "practice for a corpus run: without it only 30-minute-window categories ever get labels.",
    )
    args = parser.parse_args()

    total_start = time.monotonic()
    seed = str(args.seed)

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
          f"{batch_loop_done - total_start:.1f}s]", flush=True)

    _run("simulate outcomes", ["seed.simulate_world", "--seed", seed])
    _run("attribution", ["app.recovery.attribution", "--seed", seed])

    export_stdout = _run("export", ["app.scoring.export", "--seed", seed, "--out", "data/"])

    total_elapsed = time.monotonic() - total_start
    print(f"\n=== RESUME TOTAL: {total_elapsed:.1f}s ({total_elapsed / 60:.1f} min) ===")
    print(export_stdout)


if __name__ == "__main__":
    main()
