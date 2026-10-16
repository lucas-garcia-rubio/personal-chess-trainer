# Desktop browser contracts

Run from the repository root after installing the Python dev dependencies:

```sh
npm install --prefix tests/browser
cd tests/browser && npx playwright install chromium && cd ../..
RUN_BROWSER_TESTS=1 python -m pytest tests/browser -q
```

When using the shared Python environment from a worktree, set `PYTHONPATH` to
that worktree's `src` directory. The application currently loads Chessground,
chess.js and HTMX from jsDelivr; browser runs require access to that CDN as well
as permission to open local sockets and launch Chromium.

`CHROMIUM_PATH` can select an installed Chromium executable. `PLAYWRIGHT_MODULE`
can select an existing Playwright Node package (absolute directory path) instead
of installing another copy. Browser failures are failures, not automatic skips;
only omission of `RUN_BROWSER_TESTS=1` skips these opt-in tests.

The `desktop_app` fixture starts the real application on an ephemeral loopback
port with a fresh SQLite database, versioned Lichess data and a controlled Trainer.
It seeds a long Discussion and a three-move Variation through public HTTP routes.
Other browser scenarios can override `desktop_runtime` to control generation or
use the returned base URL with their own Playwright scripts. No external model,
user database, or live Lichess account is used.

The workspace contract runs at 1280×600, 1280×768 and 1280×1000. It measures real
board/control/composer rectangles, scrolls Critical Moments and Discussion
history independently, changes the selected Variation Position, toggles tabs,
and checks retained reading positions and draft text. It also checks that long
explanations have no nested scrollers and that opening the evaluation chart does
not resize the board. Selectors use public labels and roles, not CSS classes.
Screenshots are saved under the pytest temporary directory on success and failure;
assertion output includes geometry on visibility failures.

These tests establish desktop geometry and tab restoration. Streaming tail
following, response grouping, move following, keyboard composition, and background
Discussion isolation have separate owners in the spec 32 verification plan.
