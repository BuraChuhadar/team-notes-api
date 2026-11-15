from fastapi.testclient import TestClient

from app.main import app
from test_database import PostgreSQLTestCase


class StartupTests(PostgreSQLTestCase):
    def test_startup_applies_migrations_and_health_endpoint_responds(self) -> None:
        with TestClient(app) as client:
            response = client.get("/health")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"status": "ok"})
