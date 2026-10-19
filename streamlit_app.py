"""Streamlit Cloud runs this file for this repo; the app itself is app.py."""

import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), run_name="__main__")
