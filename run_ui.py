"""
Launcher for CRAM Streamlit Inspection UI.
Run: python -m streamlit run run_ui.py
"""
import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from cram.ui.app import main

# Invoke the application directly so Streamlit executes all components on every rerun
main()
