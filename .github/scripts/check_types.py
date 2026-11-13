"""Fail on new mypy diagnostics while tracking existing technical debt."""
from collections import Counter
from pathlib import Path
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(os.environ.get("MYPY_BASELINE_DIR", ROOT / ".ci-base")).resolve()
COMMAND = [
    sys.executable, "-m", "mypy", "--python-version", "3.10",
    "--ignore-missing-imports", "--follow-imports=silent", "--no-site-packages",
    "--no-incremental", "--no-error-summary", "--no-pretty",
    "maa_analyzer", "astrbot_plugin", "main.py",
]


def diagnostics(output: str) -> Counter[str]:
    # Ignore line numbers so unrelated edits do not invalidate the baseline.
    return Counter(re.sub(r":\d+: error:", ": error:", line)
                   for line in output.splitlines() if ": error:" in line)


def main() -> int:
    result = subprocess.run(COMMAND, cwd=ROOT, text=True, capture_output=True)
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    if result.returncode not in (0, 1):
        return result.returncode
    current = diagnostics(result.stdout)
    if result.returncode == 1 and not current:
        print("mypy failed without recognizable diagnostics", file=sys.stderr)
        return 1
    if not (BASELINE / "maa_analyzer").is_dir():
        print(f"Baseline checkout not found: {BASELINE}", file=sys.stderr)
        return 1
    previous = subprocess.run(COMMAND, cwd=BASELINE, text=True, capture_output=True)
    if previous.returncode not in (0, 1):
        print(previous.stdout, end="", file=sys.stderr)
        print(previous.stderr, end="", file=sys.stderr)
        return previous.returncode
    baseline = diagnostics(previous.stdout)
    if previous.returncode == 1 and not baseline:
        print("Baseline mypy failed without recognizable diagnostics", file=sys.stderr)
        return 1
    added = current - baseline
    if added:
        print("\nNew type errors:", file=sys.stderr)
        for item, count in sorted(added.items()):
            print(f"  {item} (count: {count})", file=sys.stderr)
        return 1
    resolved = baseline - current
    print(f"Type check passed: {sum(current.values())} existing errors; "
          f"{sum(resolved.values())} baseline errors resolved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
