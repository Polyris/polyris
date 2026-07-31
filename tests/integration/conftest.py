"""
Integration Tests - conftest.py

End-to-end tests that exercise SDK → Lambda → DynamoDB → Step Functions flows.
These tests may require AWS credentials or LocalStack.

Path setup: adds repo root to sys.path.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
