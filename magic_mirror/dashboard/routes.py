"""Pass 1 dashboard and health routes."""

import ipaddress

from flask import Blueprint, abort, current_app, jsonify, render_template, request

from ..assistant.display import read_snapshot

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


@dashboard.get("/api/assistant")
def assistant_state():
    """Current exchange only, available to a browser on this Pi's loopback."""
    try:
        local_peer = ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        local_peer = False
    local_host = (request.host.split(":", 1)[0] in {"localhost", "127.0.0.1"} or
                  request.host == "[::1]" or request.host.startswith("[::1]:"))
    if not local_peer or not local_host or request.headers.get("Sec-Fetch-Site") not in {None, "none", "same-origin"}:
        abort(403)
    settings = current_app.config["MIRROR_SETTINGS"]
    if not settings.wake_enabled:
        snapshot = {"state": "disabled"}
    else:
        try:
            snapshot = read_snapshot()
        except (OSError, ValueError, UnicodeError):
            snapshot = {"state": "unavailable"}
    response = jsonify(snapshot)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
