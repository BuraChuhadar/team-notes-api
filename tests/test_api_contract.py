"""Keep the published OpenAPI contract aligned with actual HTTP behavior."""

import unittest
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx2 import Response
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate

from app.main import app
from test_database import PostgreSQLTestCase

HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
PUBLIC_OPERATIONS = {
    ("/health", "get"),
    ("/ready", "get"),
    ("/auth/register", "post"),
    ("/auth/login", "post"),
}


def operations(document: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if method in HTTP_METHODS:
                yield path, method, operation


def validate_json_body(
    document: dict[str, Any], path: str, method: str, response: Response
) -> None:
    response_schema = document["paths"][path][method]["responses"][
        str(response.status_code)
    ]
    body_schema = response_schema["content"]["application/json"]["schema"]
    # OpenAPI component references resolve against the whole document. Include
    # that same component scope when validating the response's JSON Schema.
    Draft202012Validator(
        {"components": document["components"], **body_schema},
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(response.json())


class OpenAPIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = app.openapi()

    def setUp(self) -> None:
        # No context manager: these contract checks deliberately do not start a
        # database. The integration class below exercises application lifespan.
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_openapi_is_valid_and_matches_the_served_document(self) -> None:
        validate(self.document)
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), self.document)
        self.assertEqual(self.client.get("/docs").status_code, 200)
        self.assertEqual(self.client.get("/redoc").status_code, 200)

    def test_operations_have_explicit_unique_ids_and_use_documented_tags(self) -> None:
        declared_tags = {tag["name"] for tag in self.document["tags"]}
        self.assertTrue(all(tag.get("description") for tag in self.document["tags"]))
        operation_ids = []
        for path, method, operation in operations(self.document):
            with self.subTest(path=path, method=method):
                operation_ids.append(operation["operationId"])
                self.assertRegex(operation["operationId"], r"^[a-z][a-z0-9_]*$")
                self.assertTrue(operation.get("summary"))
                self.assertTrue(operation.get("description"))
                self.assertTrue(operation.get("tags"))
                self.assertLessEqual(set(operation["tags"]), declared_tags)
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        for route in app.routes:
            if isinstance(route, APIRoute) and route.include_in_schema:
                self.assertIsInstance(route.operation_id, str, route.path)
                self.assertTrue(route.operation_id, route.path)

    def test_bearer_requirements_and_challenges_are_documented(self) -> None:
        bearer = self.document["components"]["securitySchemes"]["BearerAuth"]
        self.assertEqual(bearer["type"], "http")
        self.assertEqual(bearer["scheme"], "bearer")
        self.assertEqual(bearer["bearerFormat"], "opaque")
        self.assertTrue(bearer.get("description"))
        for path, method, operation in operations(self.document):
            with self.subTest(path=path, method=method):
                if (path, method) in PUBLIC_OPERATIONS:
                    self.assertFalse(operation.get("security"))
                else:
                    self.assertIn({"BearerAuth": []}, operation["security"])
                    challenge = operation["responses"]["401"]["headers"][
                        "WWW-Authenticate"
                    ]
                    self.assertEqual(challenge["schema"]["type"], "string")

    def test_private_responses_and_internal_failures_have_defined_contracts(
        self,
    ) -> None:
        for path, method, operation in operations(self.document):
            with self.subTest(path=path, method=method):
                failure = operation["responses"]["500"]
                self.assertEqual(
                    failure["content"]["application/json"]["schema"]["$ref"],
                    "#/components/schemas/ErrorResponse",
                )
                if path == "/health":
                    continue
                for response in operation["responses"].values():
                    cache_header = response["headers"]["Cache-Control"]
                    self.assertEqual(cache_header["schema"]["type"], "string")
                for status_code, response in operation["responses"].items():
                    if status_code.isdecimal() and 400 <= int(status_code) < 500:
                        expected = (
                            "ValidationErrorResponse"
                            if status_code == "422"
                            else "ErrorResponse"
                        )
                        self.assertEqual(
                            response["content"]["application/json"]["schema"]["$ref"],
                            "#/components/schemas/" + expected,
                        )

    def test_known_business_errors_are_documented(self) -> None:
        expected = {
            ("/auth/register", "post"): {"409", "422"},
            ("/auth/login", "post"): {"401", "422"},
            ("/auth/me", "get"): {"401"},
            ("/auth/logout", "post"): {"401"},
            ("/notes", "post"): {"401", "404", "422"},
            ("/notes", "get"): {"401", "422"},
            ("/notes/{note_id}", "get"): {"401", "404", "422"},
            ("/notes/{note_id}", "put"): {"401", "404", "409", "422"},
            ("/notes/{note_id}", "delete"): {"401", "404", "409", "422"},
            ("/notes/{note_id}/shares", "post"): {"401", "404", "409", "422"},
            ("/notes/{note_id}/shares", "get"): {"401", "404", "422"},
            ("/notes/{note_id}/shares/{team_id}", "get"): {"401", "404", "422"},
            ("/notes/{note_id}/shares/{team_id}", "delete"): {"401", "404", "422"},
            ("/teams", "post"): {"401", "422"},
            ("/teams", "get"): {"401", "422"},
            ("/teams/{team_id}/members", "get"): {"401", "404", "422"},
            ("/teams/{team_id}/members", "post"): {"401", "403", "404", "409", "422"},
            ("/teams/{team_id}/members/{user_id}", "delete"): {
                "401",
                "403",
                "404",
                "409",
                "422",
            },
        }
        for (path, method), statuses in expected.items():
            with self.subTest(path=path, method=method):
                self.assertLessEqual(
                    statuses, set(self.document["paths"][path][method]["responses"])
                )

    def test_validation_schema_excludes_submitted_values_and_passwords_are_write_only(
        self,
    ) -> None:
        schemas = self.document["components"]["schemas"]
        issue = schemas["ValidationIssue"]
        self.assertEqual(set(issue["properties"]), {"type", "loc", "msg"})
        self.assertEqual(set(issue["required"]), {"type", "loc", "msg"})
        self.assertFalse(issue["additionalProperties"])
        self.assertEqual(
            schemas["ValidationErrorResponse"]["properties"]["detail"]["items"]["$ref"],
            "#/components/schemas/ValidationIssue",
        )
        for name in ("RegisterRequest", "LoginRequest"):
            self.assertTrue(schemas[name]["properties"]["password"]["writeOnly"])
        self.assertFalse(
            {"password", "password_hash", "token_hash"}
            & set(schemas["UserResponse"]["properties"])
        )

    def test_empty_responses_and_creation_locations_match_http_semantics(self) -> None:
        for path, method, operation in operations(self.document):
            with self.subTest(path=path, method=method):
                for status_code, response in operation["responses"].items():
                    if status_code == "204":
                        self.assertNotIn("content", response)
                    elif status_code in {"200", "201"}:
                        self.assertIn("schema", response["content"]["application/json"])
        for path in ("/notes", "/notes/{note_id}/shares"):
            created_resource = self.document["paths"][path]["post"]["responses"]["201"]
            self.assertIn("Location", created_resource["headers"])
        for path in ("/auth/register", "/teams", "/teams/{team_id}/members"):
            self.assertNotIn(
                "Location",
                self.document["paths"][path]["post"]["responses"]["201"].get(
                    "headers", {}
                ),
            )

    def test_collection_pagination_is_bounded_and_explained(self) -> None:
        for path in (
            "/notes",
            "/teams",
            "/teams/{team_id}/members",
            "/notes/{note_id}/shares",
        ):
            with self.subTest(path=path):
                operation = self.document["paths"][path]["get"]
                parameters = {
                    parameter["name"]: parameter
                    for parameter in operation["parameters"]
                }
                limit = parameters["limit"]["schema"]
                offset = parameters["offset"]["schema"]
                self.assertEqual(
                    (limit["default"], limit["minimum"], limit["maximum"]), (50, 1, 100)
                )
                self.assertEqual(
                    (offset["default"], offset["minimum"], offset["maximum"]),
                    (0, 0, 2**63 - 1),
                )
                self.assertTrue(parameters["limit"].get("description"))
                self.assertTrue(parameters["offset"].get("description"))
                if "q" in parameters:
                    self.assertTrue(parameters["q"].get("description"))
                self.assertEqual(
                    operation["responses"]["200"]["content"]["application/json"][
                        "schema"
                    ]["type"],
                    "array",
                )

    def test_unauthenticated_http_responses_match_the_security_contract(self) -> None:
        for path, method in (
            ("/auth/me", "get"),
            ("/auth/logout", "post"),
            ("/notes", "get"),
            ("/teams", "get"),
        ):
            with self.subTest(path=path, method=method):
                response = self.client.request(method, path)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                validate_json_body(self.document, path, method, response)

    def test_validation_http_response_matches_the_sanitized_schema(self) -> None:
        password = "submitted-secret-" * 10
        response = self.client.post(
            "/auth/register",
            json={
                "email": "reader@example.com",
                "display_name": "Reader",
                "password": password,
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn(password, response.text)
        for issue in response.json()["detail"]:
            self.assertEqual(set(issue), {"type", "loc", "msg"})
        validate_json_body(self.document, "/auth/register", "post", response)

    def test_unexpected_failures_return_safe_documented_json(self) -> None:
        secret = "database-password-must-not-leak"
        with (
            patch("app.auth.try_consume_login_attempt"),
            patch("app.auth.get_session", side_effect=RuntimeError(secret)),
        ):
            response = self.client.post(
                "/auth/login",
                json={"email": "reader@example.com", "password": "example-passphrase"},
            )
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json(), {"detail": "Internal server error"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn(secret, response.text)
        validate_json_body(self.document, "/auth/login", "post", response)

    def test_routing_errors_preserve_json_and_method_headers(self) -> None:
        missing = self.client.get("/not-a-resource")
        self.assertEqual(missing.status_code, 404)
        self.assertIsInstance(missing.json()["detail"], str)
        wrong_method = self.client.post("/health")
        self.assertEqual(wrong_method.status_code, 405)
        self.assertIn(
            "GET",
            {method.strip() for method in wrong_method.headers["Allow"].split(",")},
        )
        self.assertIsInstance(wrong_method.json()["detail"], str)

    def test_method_not_allowed_advertises_all_supported_resource_methods(self) -> None:
        for path, expected_methods in (
            ("/notes", {"GET", "POST"}),
            ("/notes/1", {"DELETE", "GET", "PUT"}),
            ("/notes/1/shares/1", {"DELETE", "GET"}),
        ):
            with self.subTest(path=path):
                response = self.client.patch(path)
                self.assertEqual(response.status_code, 405, response.text)
                self.assertEqual(
                    response.headers["Allow"], ", ".join(sorted(expected_methods))
                )
                self.assertEqual(response.json(), {"detail": "Method Not Allowed"})
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_health_response_matches_its_literal_schema(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "ok"})
        validate_json_body(self.document, "/health", "get", response)


class OpenAPIDatabaseContractTests(PostgreSQLTestCase):
    def test_created_resource_and_session_responses_follow_the_published_schema(
        self,
    ) -> None:
        with TestClient(app) as client:
            document = client.get("/openapi.json").json()
            registered = client.post(
                "/auth/register",
                json={
                    "email": "contract@example.com",
                    "display_name": "Contract",
                    "password": "contract-passphrase",
                },
            )
            self.assertEqual(registered.status_code, 201, registered.text)
            validate_json_body(document, "/auth/register", "post", registered)
            login = client.post(
                "/auth/login",
                json={
                    "email": "contract@example.com",
                    "password": "contract-passphrase",
                },
            )
            self.assertEqual(login.status_code, 200, login.text)
            validate_json_body(document, "/auth/login", "post", login)
            authorization = {"Authorization": "Bearer " + login.json()["access_token"]}
            created = client.post(
                "/notes", json={"title": "Contract note"}, headers=authorization
            )
            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(
                created.headers["Location"], f"/notes/{created.json()['id']}"
            )
            self.assertEqual(created.headers["Cache-Control"], "no-store")
            validate_json_body(document, "/notes", "post", created)
            fetched = client.get(created.headers["Location"], headers=authorization)
            self.assertEqual(fetched.status_code, 200, fetched.text)
            self.assertEqual(fetched.json(), created.json())
            validate_json_body(document, "/notes/{note_id}", "get", fetched)
            replacement = {
                "title": "Updated contract note",
                "content": "The HTTP body follows the documented schema.",
                "version": fetched.json()["version"],
            }
            updated = client.put(
                created.headers["Location"], json=replacement, headers=authorization
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(updated.json()["version"], replacement["version"] + 1)
            validate_json_body(document, "/notes/{note_id}", "put", updated)
            stale_update = client.put(
                created.headers["Location"], json=replacement, headers=authorization
            )
            self.assertEqual(stale_update.status_code, 409, stale_update.text)
            self.assertEqual(stale_update.headers["Cache-Control"], "no-store")
            validate_json_body(document, "/notes/{note_id}", "put", stale_update)
            stale_delete = client.delete(
                created.headers["Location"],
                params={"version": replacement["version"]},
                headers=authorization,
            )
            self.assertEqual(stale_delete.status_code, 409, stale_delete.text)
            validate_json_body(document, "/notes/{note_id}", "delete", stale_delete)
            deleted = client.delete(
                created.headers["Location"],
                params={"version": updated.json()["version"]},
                headers=authorization,
            )
            self.assertEqual(deleted.status_code, 204, deleted.text)
            self.assertEqual(deleted.content, b"")
            self.assertEqual(deleted.headers["Cache-Control"], "no-store")
            logout = client.post("/auth/logout", headers=authorization)
            self.assertEqual(logout.status_code, 204, logout.text)
            self.assertEqual(logout.content, b"")
            self.assertEqual(logout.headers["Cache-Control"], "no-store")
