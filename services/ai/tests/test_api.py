from fastapi.testclient import TestClient

from docai.api.app import create_app
from tests.service_support import fake_services


def test_health() -> None:
    with TestClient(create_app(services=fake_services())) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_tracing_is_set_up_once_at_startup() -> None:
    app = create_app(services=fake_services())
    with TestClient(app):
        assert not app.state.services.tracing.enabled
