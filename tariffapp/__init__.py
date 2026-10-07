"""Air cargo tariff web app.

The core (parsers, loader, importers, linker, queries) has no web-framework
dependency, so it's shared by the Flask app (create_app) and the Streamlit
app (streamlit_app.py).
"""

import os

from . import db

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "instance", "tariff.sqlite")


def database_path():
    return os.environ.get("TARIFFAPP_DB", DEFAULT_DB)


def create_app(test_config=None):
    from flask import Flask

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("TARIFFAPP_SECRET", "dev-change-me"),
        DATABASE=database_path(),
        UPLOAD_FOLDER=os.environ.get("TARIFFAPP_UPLOADS", os.path.join(app.instance_path, "uploads")),
        MAX_CONTENT_LENGTH=100 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        db.init_schema(conn)
        conn.close()

    app.teardown_appcontext(db.close_db)

    from . import views, cli
    app.register_blueprint(views.bp)
    cli.register(app)
    views.register_filters(app)
    return app
