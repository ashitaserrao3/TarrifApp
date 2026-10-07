"""Air cargo tariff web app."""

import os

from flask import Flask

from . import db


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("TARIFFAPP_SECRET", "dev-change-me"),
        DATABASE=os.environ.get("TARIFFAPP_DB", os.path.join(app.instance_path, "tariff.sqlite")),
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
