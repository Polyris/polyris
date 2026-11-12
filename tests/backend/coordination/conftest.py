"""Coordination harness (ADR #130, Testing): see README.md in this directory."""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
COORDINATION = REPO_ROOT / "sam" / "lambdas" / "coordination"

for path in (HERE, COORDINATION / "tests", COORDINATION):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
