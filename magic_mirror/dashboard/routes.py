"""Pass 1 dashboard and health routes."""

from flask import Blueprint, current_app, jsonify, render_template

dashboard = Blueprint("dashboard", __name__, template_folder="templates", static_folder="static", static_url_path="/dashboard-assets")


@dashboard.get("/")
def home():
    return render_template("dashboard.html", settings=current_app.config["MIRROR_SETTINGS"])


@dashboard.get("/health")
def health():
    return jsonify(status="healthy")


@dashboard.get("/api/dashboard")
def dashboard_data():
    data = current_app.config["MIRROR_DATA"]
    return jsonify(modules=data.snapshot() if data is not None else {})
