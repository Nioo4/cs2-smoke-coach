"""Fail closed on the backend coverage data produced by pytest-cov."""

from __future__ import annotations

import argparse
import io
import math
import sys
from pathlib import Path

import coverage

MINIMUM_COVERAGE = 85.0


def gate_exit_code(total: float) -> int:
    """Return zero only for a finite total at or above the fixed gate."""

    return 0 if math.isfinite(total) and total >= MINIMUM_COVERAGE else 1


def measured_total(data_file: Path) -> float:
    report = coverage.Coverage(data_file=str(data_file))
    report.load()
    return float(report.report(file=io.StringIO()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        total = measured_total(args.data_file)
    except (OSError, coverage.CoverageException):
        print("BACKEND_COVERAGE_GATE_ERROR", file=sys.stderr)
        return 2
    print(f"BACKEND_COVERAGE_GATE total={total:.2f} minimum={MINIMUM_COVERAGE:.2f}")
    return gate_exit_code(total)


if __name__ == "__main__":
    raise SystemExit(main())
