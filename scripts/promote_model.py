#!/usr/bin/env python3
"""CLI helper to evaluate candidate fraud model and promote to MLflow registry champion (TASK-038).

Usage:
    python scripts/promote_model.py --run-id <run_id>
    python scripts/promote_model.py --latest-run --experiment pbip-fraud
"""

import sys

from pbip_ml.fraud.promotion import main

if __name__ == "__main__":
    sys.exit(main())
