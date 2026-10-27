from magic_mirror import create_app
from magic_mirror.config import Settings


def test_homepage():
    response = create_app(Settings()).test_client().get("/")
    assert response.status_code == 200
    assert b"Magic Mirror V2" in response.data
    assert b"Application status: Running" in response.data


def test_dashboard_regions_and_local_assets():
    app = create_app(Settings())
    response = app.test_client().get("/")
    for name in ("weather", "news", "pihole", "system", "assistant"):
        assert f'id="module-{name}"'.encode() in response.data
    assert b'id="clock-time"' in response.data
    assert b'id="clock-date"' in response.data
    assert b"Not connected" in response.data
    assert b"Conversation will appear here" in response.data
    for asset in ("dashboard.css", "clock.js", "modules.js", "data.js"):
        assert app.test_client().get(f"/dashboard-assets/{asset}").status_code == 200


def test_disabled_modules_leave_clock_and_health_available():
    app = create_app(Settings(enabled_modules=()))
    body = app.test_client().get("/").data
    assert b'id="clock-time"' in body
    assert b'data-module=' not in body
    assert app.test_client().get("/health").json == {"status": "healthy"}


def test_clock_preference_and_selected_modules_render():
    body = create_app(Settings(clock_format="12", display_scale=1.25,
                               enabled_modules=("weather",))).test_client().get("/").data
    assert b'data-clock-format="12"' in body
    assert b'--display-scale: 1.25' in body
    assert b'id="module-weather"' in body
    assert b'id="module-news"' not in body


def test_health():
    response = create_app(Settings()).test_client().get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "healthy"}


def test_initialization_does_not_bind_network(monkeypatch):
    import socket

    def fail_bind(*args, **kwargs):
        raise AssertionError("application factory opened a network socket")

    monkeypatch.setattr(socket.socket, "bind", fail_bind)
    app = create_app(Settings())
    assert app.test_client().get("/health").status_code == 200


def test_unknown_route_returns_404():
    response = create_app(Settings()).test_client().get("/missing")
    assert response.status_code == 404
