"""Pass 1 dashboard and health routes."""

from flask import Blueprint, jsonify, render_template

dashboard = Blueprint("dashboard", __name__, template_folder="templates")


@dashboard.get("/")
def home():
    return render_template("dashboard.html", status="Running")


@dashboard.get("/health")
def health():
    return jsonify(status="healthy")
