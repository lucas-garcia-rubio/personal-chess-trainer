from collections.abc import Callable, Iterator
from html import escape

import pytest
from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from test_trainer_response import FakeOpenCode
from test_trainer_stream import URL, events
from trainer.application.ports import ResponseControl, TrainerEvent
from trainer.bootstrap import create_app
from trainer.config import Settings


@pytest.mark.parametrize('source, expected', [
    ('Lance | Plano\n--- | ---\nNf3 | Centro\n',
     'Lance — Plano\nNf3 — Centro\n'),
    ('![Position](https://example.test/board.png)',
     'Position (https://example.test/board.png)'),
    ('**Calcule Qh7#', 'Calcule Qh7#'),
    ('# Qh7#\n## Qh7# ##\n', 'Qh7#\nQh7#\n'),
    ('Plano\n=====\n> Desenvolva __Nf3__\n- [x] Roque\n\n***\n',
     'Plano\nDesenvolva Nf3\nRoque\n\n'),
    ('A chave pawn_structure preserva o texto.', 'A chave pawn_structure preserva o texto.'),
    ('1. e4 e5 2. Nf3!? Nc6 3... Qh7#\nO-O O-O-O exd8=Q+ 1-0 0-1 1/2-1/2 *',
     '1. e4 e5 2. Nf3!? Nc6 3... Qh7#\nO-O O-O-O exd8=Q+ 1-0 0-1 1/2-1/2 *'),
    ('<script>secret</script>**Plano** &amp; <b>Nf3</b>', 'Plano & Nf3'),
    ('## Plano\n\n**Desenvolva** com *Nf3*.\n- Preserve `Qh7#` e O-O.\n',
     'Plano\n\nDesenvolva com Nf3.\nPreserve Qh7# e O-O.\n'),
    ('| Lance | Plano |\n| --- | :---: |\n| **Nf3** | Centro |\n'
     '\n```chess\n1. e4 e5 2. Nf3 Nc6\n```\n[Estudo](https://example.test)\n'
     '1. Desenvolva\n2. Proteja o rei\n',
     'Lance — Plano\nNf3 — Centro\n'
     '\n1. e4 e5 2. Nf3 Nc6\nEstudo (https://example.test)\n'
     'Desenvolva\nProteja o rei\n'),
])
@pytest.mark.parametrize('fragmented', [False, True])
def test_new_response_is_plain_text_in_stream_replay_and_reopen(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
    fragmented: bool, source: str, expected: str,
) -> None:

    class MarkdownRuntime(FakeOpenCode):
        def stream(self, session_id: str, system: str, context: str,
                   control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
            for chunk in list(source) if fragmented else [source]:
                yield TrainerEvent('text', chunk)
            assert 'plain text' in system
            assert 'short paragraphs' in system
            assert 'no\nheadings, bold, Markdown lists, tables' in system
            yield TrainerEvent('completed')

    settings = migrated_settings('Lance5500')
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for('Lance5500'),
                               trainer_runtime=MarkdownRuntime())) as client:
        client.post('/sync')
        client.post('/trainer/consent', json={'accepted': True})
        attempt = client.post(URL, data={'base_ply': '0', 'message': 'Explain', 'stream': 'true'}).json()
        output = events(client, attempt['eventsUrl'])
        text = ''
        for event in output:
            if event['type'] == 'text':
                text += event['text']
                assert expected.startswith(text)
        assert text == expected
        assert output[-1]['type'] == 'completed'
        cursor = output[len(output) // 2]['sequence']
        assert events(client, attempt['eventsUrl'] + f'?after={cursor}') == [
            event for event in output if event['sequence'] > cursor
        ]
        rendered = events(client, attempt['eventsUrl'] + '?render=true')
        assert escape(expected) in rendered[-1]['html']

    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request('offline'))) as client:
        assert client.get(URL + '/0').json()['messages'][-1]['body'] == expected
        assert events(client, attempt['eventsUrl']) == output
        fragment = client.get(URL + '/0/fragment').text
        assert escape(expected) in fragment


def test_plain_text_is_visible_while_response_is_still_running(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    from threading import Event

    class PausedRuntime(FakeOpenCode):
        paused = Event()
        release = Event()

        def stream(self, session_id: str, system: str, context: str,
                   control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
            for chunk in ['#', '# Plano\n\n*', '*Nf3', '** desenvolve.\n', '**Ainda']:
                yield TrainerEvent('text', chunk)
            self.paused.set()
            assert self.release.wait(5)
            yield TrainerEvent('text', ' calculando**.')
            yield TrainerEvent('completed')

        def abort(self, session_id: str) -> None:
            self.release.set()

    runtime = PausedRuntime()
    with TestClient(create_app(migrated_settings('Lance5500'),
                               lichess_transport=lichess_mock.games_for('Lance5500'),
                               trainer_runtime=runtime)) as client:
        client.post('/sync')
        client.post('/trainer/consent', json={'accepted': True})
        attempt = client.post(URL, data={'base_ply': '0', 'message': 'Explain', 'stream': 'true'}).json()
        try:
            assert runtime.paused.wait(5)
            current = client.get(URL + '/0').json()
            assert current['attempts'][0]['status'] == 'running'
            assert current['messages'][-1]['body'] == 'Plano\n\nNf3 desenvolve.\n'
            fragment = client.get(URL + '/0/fragment').text
            assert 'Plano\n\nNf3 desenvolve.\n' in fragment
            assert '**' not in fragment
        finally:
            runtime.release.set()
        assert events(client, attempt['eventsUrl'])[-1]['type'] == 'completed'
        assert client.get(URL + '/0').json()['messages'][-1]['body'] == (
            'Plano\n\nNf3 desenvolve.\nAinda calculando.'
        )


def test_historical_markdown_and_player_text_are_not_converted(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    from trainer.adapters.sqlite import SQLiteStorage

    settings = migrated_settings('Lance5500')
    historical = '**Historical Qh7#**'
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for('Lance5500'))) as client:
        client.post('/sync')
        client.post(URL, data={'base_ply': '0', 'message': '**Player text**'})
    # Seed a pre-policy persisted response through the storage boundary.
    storage = SQLiteStorage(settings.database_path)
    prior = storage.begin_response('lichess', 'q7ZvsdUF', 0)
    storage.record_response_event(prior.id, TrainerEvent('text', historical))
    storage.record_response_event(prior.id, TrainerEvent('completed'))
    storage.close()
    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request('offline'),
                               trainer_runtime=FakeOpenCode())) as client:
        original = client.get(URL + '/0').json()['messages']
        assert original == [{'author': 'player', 'body': '**Player text**'},
                            {'author': 'trainer', 'body': historical}]
        assert historical in client.get(URL + '/0/fragment').text
        client.post('/trainer/consent', json={'accepted': True})
        client.post(URL, data={'base_ply': '0', 'message': 'Continue'})
        assert client.get(URL + '/0').json()['messages'][:2] == original


@pytest.mark.parametrize('termination', ['exception', 'interrupted', 'failed', 'limited', 'steps'])
def test_provider_failure_preserves_plain_text_from_unfinished_markdown(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
    termination: str,
) -> None:
    class FailingRuntime(FakeOpenCode):
        def stream(self, session_id: str, system: str, context: str,
                   control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
            yield TrainerEvent('text', '**Keep Qh7#')
            if termination == 'exception':
                raise RuntimeError('PRIVATE_PROVIDER_ERROR')
            if termination == 'steps':
                for _ in range(8):
                    yield TrainerEvent('step')
            else:
                yield TrainerEvent(termination)

    with TestClient(create_app(migrated_settings('Lance5500'),
                               lichess_transport=lichess_mock.games_for('Lance5500'),
                               trainer_runtime=FailingRuntime())) as client:
        client.post('/sync')
        client.post('/trainer/consent', json={'accepted': True})
        attempt = client.post(URL, data={'base_ply': '0', 'message': 'Explain', 'stream': 'true'}).json()
        output = events(client, attempt['eventsUrl'])
        expected_status = {'exception': 'failed', 'steps': 'limited'}.get(termination, termination)
        assert output[-1]['type'] == expected_status
        assert client.get(URL + '/0').json()['messages'][-1]['body'] == 'Keep Qh7#'
        assert '**' not in client.get(URL + '/0/fragment').text
