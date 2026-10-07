#!/usr/bin/env python3
"""
Start the Cargo Tariffs web app.

    python run.py                  # just you:   http://localhost:5000
    python run.py --host 0.0.0.0   # your team:  http://<this-pc's-name>:5000

Uses the 'waitress' production server when it's installed, otherwise
Flask's built-in server.
"""

import argparse

from tariffapp import create_app

ap = argparse.ArgumentParser(description="Run the Cargo Tariffs web app.")
ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to let other PCs on your network connect")
ap.add_argument("--port", type=int, default=5000)
args = ap.parse_args()

app = create_app()
print(f"Cargo Tariffs running on http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}")
print(f"Database: {app.config['DATABASE']}")
try:
    from waitress import serve
    serve(app, host=args.host, port=args.port)
except ImportError:
    app.run(host=args.host, port=args.port)
