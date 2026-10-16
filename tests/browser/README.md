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

The composer contract uses a gated stream to check Enter, Shift+Enter and IME
composition, editing while a send is pending or a response is streaming, focus
preservation, and explicit sending after completion or interruption. It also
checks keyboard access to consent settings and revocation through the public HTTP
routes. Run it alone with `RUN_BROWSER_TESTS=1 python -m pytest
tests/browser/test_composer.py -q`.

`test_evidence.py` checks initially collapsed Evaluator consultations within a
Trainer response, readable Position/line references, verified and unavailable
results, and secondary technical details. It releases a controlled response
while the Player has evaluations open to verify that streaming preserves their
expansion. Reloading starts collapsed again and retains the consultation content.

`test_follow.py` controls a demonstration through the public Trainer tools while
a real browser inspects earlier moves, steps backward and forward, opens the Base
Position and returns to the Game. It checks explicit resumption, preserved reading
position, absence of selection writes during inspection, and the Position actually
received by the Trainer on the next send. `tests/test_inspected_position.py` also
checks that next-send contract over HTTP, including unchanged Game, Analysis, Base
Position, Variation nodes and prior Discussion messages.
Streaming tail following and background Discussion isolation
have separate owners in the spec 32 verification plan.
