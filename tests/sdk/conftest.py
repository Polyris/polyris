"""
SDK Tests - conftest.py

Tests for the polyris Python SDK (DAG definitions, ASL generation, templates).
These tests validate the library layer and can run independently of infrastructure.

Path setup: adds repo root to sys.path so `from polyris import ...` works.
"""
import os
import sys

# Repo root is two levels up from tests/sdk/
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
