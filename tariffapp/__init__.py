"""Air cargo tariff engine: rate-file parsing, schedule import, flight-to-rate
matching. The Streamlit dashboard (streamlit_app.py) sits on top of it."""

import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "instance", "tariff.sqlite")


def database_path():
    return os.environ.get("TARIFFAPP_DB", DEFAULT_DB)
