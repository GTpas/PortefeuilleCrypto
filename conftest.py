"""Ensures the repository root is importable during tests (matches the
PYTHONPATH=. convention used to run the workers)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
