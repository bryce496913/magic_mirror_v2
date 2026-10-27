"""Flask application factory."""

from flask import Flask

from .config import Settings
from .dashboard.routes import dashboard


def create_app(settings: Settings | None = None, data=None) -> Flask:
    app = Flask(__name__)
    app.config["MIRROR_SETTINGS"] = settings if settings is not None else Settings.from_env()
    app.config["MIRROR_DATA"] = data
    app.register_blueprint(dashboard)
    return app
