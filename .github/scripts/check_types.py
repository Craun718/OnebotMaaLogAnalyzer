"""Fail on new mypy diagnostics while tracking existing technical debt."""
from collections import Counter
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / ".github" / "mypy-baseline.txt"
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
    baseline = Counter(BASELINE.read_text(encoding="utf-8").splitlines())
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
