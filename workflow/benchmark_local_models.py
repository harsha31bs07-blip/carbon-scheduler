"""Deprecated command alias for production-workload calibration.

The former separate benchmark used fixed prompts, nested trackers and a default
US grid assumption. New runs use workflow.calibrate and preserve every trial.
Historical benchmark artifacts are unchanged.
"""

from __future__ import annotations

from pathlib import Path
import sys

# Keep direct script invocation compatible as well as python -m invocation.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from workflow.calibrate import main as calibrate_main

    print("workflow.benchmark_local_models is deprecated; forwarding to python -m workflow.calibrate.",
          file=sys.stderr)
    return calibrate_main()


if __name__ == "__main__":
    raise SystemExit(main())
