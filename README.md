# personal-chess-trainer

Aplicativo web local para revisar as partidas de xadrez do Player.

## Desenvolvimento

O projeto requer Python 3.12 ou posterior e usa uma virtual environment local:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Crie `config.toml` na raiz do projeto:

```toml
[lichess]
username = "seu-username"

[database]
path = "data/trainer.db"
```

Caminhos relativos do banco são resolvidos a partir da pasta que contém o
arquivo de configuração. Inicie a aplicação com:

```bash
trainer serve
```

A Home estará disponível em <http://127.0.0.1:8000>.

## Verificação

```bash
python -m mypy src tests
python -m pytest
```

Testes que executam Sync devem receber a fixture `lichess_mock` e passar
`lichess_mock.games_for(username)` como `lichess_transport` para `create_app`.
O transporte responde com o NDJSON versionado em
`tests/fixtures/lichess_game.ndjson`, registra as requisições em
`lichess_mock.requests` e falha localmente para endpoints inesperados. Para
confirmar que uma página persistida abre offline, use
`lichess_mock.fail_on_request(reason)` ao reconstruir a aplicação.
