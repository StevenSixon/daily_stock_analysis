#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run one or continuously poll PEI Research Worker jobs."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.research_worker import ResearchWorker  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the external PEI Research Worker")
    parser.add_argument("--once", action="store_true", help="Claim at most one job and exit")
    parser.add_argument("--verbose", action="store_true", help="Enable informational Worker logs")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    worker = ResearchWorker(project_root=PROJECT_ROOT)
    if args.once:
        return 0 if worker.run_once() else 3
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
