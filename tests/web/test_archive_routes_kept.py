"""Archive and restore keep their addresses, methods and CSRF check when they share one path (0.4.7, R6)."""

from __future__ import annotations

from conftest import api_routes
from reprintbuddy.web.csrf import verify_csrf

EXPECTED = {
    ("/parts/{part_id:int}/archive", "POST"),
    ("/parts/{part_id:int}/restore", "POST"),
    ("/sets/{set_id:int}/archive", "POST"),
    ("/sets/{set_id:int}/restore", "POST"),
    ("/articles/{article_id:int}/archive", "POST"),
    ("/articles/{article_id:int}/restore", "POST"),
}


def _archive_routes(app):
    return [
        route for route in api_routes(app)
        if route.path.endswith(("/archive", "/restore")) and not route.path.startswith("/settings")
    ]


def test_archive_and_restore_routes_stay(client):
    found = {(route.path, method) for route in _archive_routes(client.app) for method in route.methods}
    assert found == EXPECTED


def test_archive_and_restore_check_the_csrf_token(client):
    for route in _archive_routes(client.app):
        assert any(dep.call is verify_csrf for dep in route.dependant.dependencies), route.path
