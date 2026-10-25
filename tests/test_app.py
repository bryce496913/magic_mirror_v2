from magic_mirror import create_app
from magic_mirror.config import Settings


def test_homepage():
    response = create_app(Settings()).test_client().get("/")
    assert response.status_code == 200
    assert b"Magic Mirror V2" in response.data
    assert b"Application status: Running" in response.data


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
