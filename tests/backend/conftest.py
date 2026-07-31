"""
Backend Tests - conftest.py

Tests for the backend API layer (Lambda handlers, routes, alerting).
These tests validate the deployed service logic and can run independently.

Path setup: adds console_api lambda dir and repo root to sys.path.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONSOLE_API = os.path.join(REPO_ROOT, 'sam', 'lambdas', 'console_api')

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if CONSOLE_API not in sys.path:
    sys.path.insert(0, CONSOLE_API)
