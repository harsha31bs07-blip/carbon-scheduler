"""Deprecated command alias for production-workload calibration.

Historical result files keep their original meaning. New runs use the connected
workflow prompts, deployment adapters and retained-trial protocol.
"""

from __future__ import annotations

import sys


def main():
    from workflow.calibrate import main as calibrate_main

    print("full_benchmark.py is deprecated; forwarding to python -m workflow.calibrate.",
          file=sys.stderr)
    return calibrate_main()


if __name__ == "__main__":
    raise SystemExit(main())
