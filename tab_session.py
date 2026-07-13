#!/usr/bin/env python3
"""Backward-compatibility wrapper — delegates to session_manager.py.

This file is kept for scripts that still reference tab_session.py directly.
New code should use session_manager.py."""

import sys
import os

# Add the same directory to path so we can import the new module
_here = os.path.dirname(os.path.abspath(__file__))
_sm = os.path.join(_here, "session_manager.py")

# Simply re-execute with session_manager.py and same args
sys.argv[0] = _sm
exec(open(_sm, encoding="utf-8").read())
