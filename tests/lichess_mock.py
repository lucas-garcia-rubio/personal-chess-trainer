from pathlib import Path
from urllib.parse import quote

import httpx


class LichessMock:
    def __init__(self, fixture: str) -> None:
        self.fixture = fixture
        self.requests: list[httpx.Request] = []

    def games_for(
        self,
        username: str,
        *,
        ndjson: str | None = None,
    ) -> httpx.MockTransport:
        expected_path = f"/api/games/user/{quote(username, safe='')}"
        response_body = self.fixture if ndjson is None else ndjson

        def handle(request: httpx.Request) -> httpx.Response:
            actual_path = request.url.raw_path.split(b"?", maxsplit=1)[0].decode()
            assert request.method == "GET", (
                f"Unexpected Lichess request method: {request.method}"
            )
            assert request.url.scheme == "https" and request.url.host == "lichess.org", (
                f"Unexpected Lichess request origin: {request.url}"
            )
            assert actual_path == expected_path, (
                f"Unexpected Lichess request path: {actual_path}; "
                f"expected {expected_path}"
            )
            self.requests.append(request)
            return httpx.Response(
                200,
                text=response_body,
                headers={"content-type": "application/x-ndjson"},
            )

        return httpx.MockTransport(handle)

    def fail_on_request(self, reason: str) -> httpx.MockTransport:
        def fail(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"{reason}: {request.method} {request.url}")

        return httpx.MockTransport(fail)


def load_lichess_fixture() -> str:
    return (Path(__file__).parent / "fixtures" / "lichess_game.ndjson").read_text(
        encoding="utf-8"
    )
