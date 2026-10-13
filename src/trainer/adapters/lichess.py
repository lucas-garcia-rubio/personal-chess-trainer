from urllib.parse import quote

import httpx


class LichessGameSource:
    def __init__(
        self,
        username: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._username = username
        self._client = httpx.Client(
            base_url="https://lichess.org",
            headers={
                "User-Agent": (
                    "personal-chess-trainer/0.1 "
                    "(+https://github.com/lucas-garcia-rubio/personal-chess-trainer)"
                )
            },
            transport=transport,
            timeout=30,
        )

    def fetch_games(self, since: int | None = None) -> list[str]:
        params = {
            "analysed": "true",
            "evals": "true",
            "clocks": "true",
        }
        if since is not None:
            params["since"] = str(since)
        response = self._client.get(
            f"/api/games/user/{quote(self._username, safe='')}",
            params=params,
            headers={"Accept": "application/x-ndjson"},
        )
        response.raise_for_status()
        return [line for line in response.text.splitlines() if line]

    def close(self) -> None:
        self._client.close()
