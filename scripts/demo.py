"""Exercise the public notes API using only Python's standard library.

This demo creates three accounts, two teams, and one retained private note in the
chosen database. It does not remove existing data. Run against a disposable stack
when you do not want these demo records in your development database.
"""

import argparse
from dataclasses import dataclass
import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4


class DemoFailure(RuntimeError):
    """An API response did not match the documented behavior."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DemoFailure(message)


def object_response(value: Any) -> dict[str, Any]:
    require(isinstance(value, dict), "Expected a JSON object response")
    return value


def integer_field(value: dict[str, Any], name: str) -> int:
    result = value.get(name)
    if type(result) is not int or result <= 0:
        raise DemoFailure(f"Expected positive integer {name}")
    return result


@dataclass(frozen=True)
class Account:
    user_id: int
    token: str


class API:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def call(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
        expected: int = 200,
        no_store: bool = True,
        location: str | None = None,
        allow: set[str] | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=15) as response:
                status, response_headers, body = response.status, response.headers, response.read()
        except HTTPError as error:
            try:
                status, response_headers, body = error.code, error.headers, error.read()
            finally:
                error.close()
        except (URLError, TimeoutError) as error:
            raise DemoFailure(f"{method} {path}: could not reach the API") from error
        require(status == expected, f"{method} {path}: expected HTTP {expected}, got {status}")
        if no_store:
            directives = response_headers.get("Cache-Control", "").lower().split(",")
            require("no-store" in {item.strip() for item in directives}, f"{method} {path}: missing no-store")
        if expected == 401:
            require(response_headers.get("WWW-Authenticate") == "Bearer",
                    f"{method} {path}: missing bearer challenge")
        if allow is not None:
            methods = {item.strip() for item in response_headers.get("Allow", "").split(",") if item.strip()}
            require(methods == allow, f"{method} {path}: incomplete Allow header")
        if location is not None:
            require(response_headers.get("Location") == location, f"{method} {path}: incorrect Location")
        if expected == 204:
            require(body == b"", f"{method} {path}: HTTP 204 must have an empty body")
            return None
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError) as error:
            raise DemoFailure(f"{method} {path}: expected a JSON response") from error

    def account(self, name: str, run_id: str) -> Account:
        email = f"{name.lower()}.{run_id}@example.com"
        password = f"demo-{uuid4().hex}"
        registered = object_response(self.call(
            "POST", "/auth/register", expected=201,
            payload={"email": email, "display_name": name, "password": password},
        ))
        require(
            set(registered) == {"id", "email", "display_name", "created_at"},
            "Registration must return only public user fields",
        )
        logged_in = object_response(self.call(
            "POST", "/auth/login", payload={"email": email, "password": password},
        ))
        token = logged_in.get("access_token")
        if not isinstance(token, str) or not token:
            raise DemoFailure("Login did not return a bearer token")
        require(logged_in.get("token_type") == "bearer", "Login returned the wrong token type")
        return Account(user_id=integer_field(registered, "id"), token=token)


def run_demo(api: API) -> None:
    require(api.call("GET", "/health", no_store=False) == {"status": "ok"}, "Health check failed")
    run_id = uuid4().hex
    marker = f"demo-{run_id}"
    alice, bob, carol = (api.account(name, run_id) for name in ("Alice", "Bob", "Carol"))
    print("OK: registered and signed in Alice, Bob, and Carol")
    api.call("PATCH", "/notes", token=alice.token, expected=405, allow={"GET", "POST"})

    source = object_response(api.call(
        "POST", "/teams", token=alice.token, payload={"name": f"Source {run_id}"}, expected=201,
    ))
    recipient = object_response(api.call(
        "POST", "/teams", token=alice.token, payload={"name": f"Recipient {run_id}"}, expected=201,
    ))
    source_id, recipient_id = integer_field(source, "id"), integer_field(recipient, "id")
    for team_id, account in ((source_id, bob), (recipient_id, carol)):
        api.call("POST", f"/teams/{team_id}/members", token=alice.token,
                 payload={"user_id": account.user_id}, expected=201)
    print("OK: Alice created both teams and added their members")

    api.call("POST", "/notes", token=alice.token,
             payload={"title": "Invalid ownership", "owner_id": bob.user_id}, expected=422)
    note = object_response(api.call(
        "POST", "/notes", token=alice.token, expected=201,
        payload={"title": f"Shared draft {marker}", "content": marker, "team_id": source_id},
    ))
    private = object_response(api.call(
        "POST", "/notes", token=alice.token, expected=201,
        payload={"title": f"Private draft {marker}", "content": marker},
    ))
    note_id, private_id = integer_field(note, "id"), integer_field(private, "id")
    note_path = f"/notes/{note_id}"
    api.call("PATCH", note_path, token=alice.token, expected=405, allow={"GET", "PUT", "DELETE"})
    require(integer_field(note, "version") == 1, "A new note must start at version 1")
    api.call("GET", f"/notes/{private_id}", token=bob.token, expected=404)
    api.call("GET", f"/notes/{private_id}", token=carol.token, expected=404)
    updated = object_response(api.call(
        "PUT", note_path, token=bob.token,
        payload={"title": f"Updated {marker}", "content": f"Bob's decision: {marker}", "version": 1},
    ))
    require(integer_field(updated, "version") == 2, "A successful edit must increment the version")
    require(updated["owner_id"] == alice.user_id and updated["team_id"] == source_id,
            "Editing changed note ownership or its source team")
    require(updated["created_at"] == note["created_at"], "Editing changed the creation time")
    api.call("PUT", note_path, token=alice.token, expected=409,
             payload={"title": "Stale save", "content": "Must not replace Bob's work", "version": 1})
    require(api.call("GET", note_path, token=alice.token) == updated, "A rejected save changed the note")
    print("OK: Bob edited the team note; Alice's stale save was rejected with 409")

    api.call("GET", note_path, token=carol.token, expected=404)
    share_path = f"{note_path}/shares/{recipient_id}"
    api.call("POST", f"{note_path}/shares", token=alice.token,
             payload={"team_id": recipient_id}, expected=201, location=share_path)
    require(api.call("GET", note_path, token=carol.token) == updated, "Shared note did not match the saved note")
    api.call("PUT", note_path, token=carol.token, expected=404,
             payload={"title": "Forbidden save", "content": "Read-only recipient", "version": 2})
    api.call("DELETE", note_path + "?version=2", token=carol.token, expected=404)
    api.call("POST", f"{note_path}/shares", token=carol.token,
             payload={"team_id": recipient_id}, expected=404)
    search_path = "/notes?" + urlencode({"q": marker, "limit": 1, "offset": 0})
    matches = api.call("GET", search_path, token=carol.token)
    require(isinstance(matches, list) and [match["id"] for match in matches] == [note_id],
            "Search must return the shared note and exclude Alice's private note")
    next_page = "/notes?" + urlencode({"q": marker, "limit": 1, "offset": 1})
    require(api.call("GET", next_page, token=carol.token) == [], "The next search page should be empty")
    print("OK: Carol can read and search the share, but cannot edit, delete, or re-share it")

    api.call("DELETE", share_path, token=alice.token, expected=204)
    api.call("GET", note_path, token=carol.token, expected=404)
    require(api.call("GET", search_path, token=carol.token) == [], "Revocation did not remove search access")
    require(api.call("GET", note_path, token=bob.token) == updated, "Revocation removed independent source-team access")
    api.call("DELETE", note_path + "?version=2", token=bob.token, expected=204)
    api.call("GET", note_path, token=alice.token, expected=404)
    print("OK: revocation removed Carol's access; Bob deleted the current note version")

    for account in (alice, bob, carol):
        api.call("POST", "/auth/logout", token=account.token, expected=204)
        api.call("GET", "/auth/me", token=account.token, expected=401)
    print("OK: logout revoked all demo sessions; their tokens now return 401")
    print("Demo passed. Three accounts, two teams, and one private note remain in the target database.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API origin (default: %(default)s)")
    arguments = parser.parse_args()
    parsed_url = urlsplit(arguments.base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        parser.error("--base-url must be an absolute HTTP or HTTPS URL")
    print("Running the API demo; it creates records in the selected database.")
    try:
        run_demo(API(arguments.base_url))
    except DemoFailure as error:
        print(f"Demo failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
