# personal-chess-trainer

Aplicativo web local para revisar as partidas de xadrez do Player.

## Desenvolvimento

O projeto requer Python 3.12 ou posterior, Flyway `13.9.0` com suporte a SQLite,
OpenCode V2 (2.2.3 ou posterior na série 2.x) para o Trainer e usa uma virtual environment local:

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

# Opcional quando `stockfish` ja esta no PATH. Os demais valores abaixo sao os
# defaults; depth aceita 1-128, threads 1-1024, hash 1-33554432 MB,
# max_plies 1-10000 e timeout_seconds 1-3600.
[engine]
path = "/caminho/para/stockfish"
depth = 15
threads = 1
hash = 128
max_plies = 1000
timeout_seconds = 600

# Opcional quando `flyway` ja esta no PATH.
[migration]
flyway_path = "/caminho/para/flyway"

# Opcional quando `opencode` ja esta no PATH.
[opencode]
path = "/caminho/para/opencode"
startup_timeout_seconds = 10
http_timeout_seconds = 5
# Opcional: modelo inicial do Trainer, até escolher outro em Settings na Home.
# Se omitido, usa o modelo padrão do OpenCode.
model = "openai/gpt-6.1-sol"
```

Caminhos relativos do banco e dos executáveis são resolvidos a partir da pasta
que contém o arquivo de configuração. Antes de abrir o banco ou iniciar o
servidor, `trainer serve` exige exatamente Flyway `13.9.0`, valida os checksums e
aplica as migrations em `src/trainer/migrations`. O OpenCode roda em um diretório
privado ligado somente a `127.0.0.1`; sua indisponibilidade desativa apenas o
Trainer. Inicie a aplicação com:

```bash
trainer serve
```

A Home estará disponível em <http://127.0.0.1:8765>.

O botão **Settings** na Home permite escolher o modelo do Trainer. As opções vêm
de `opencode models`, usando o executável configurado em `[opencode] path` ou no
PATH. A escolha fica salva no SQLite e vale para as próximas respostas, inclusive
em Discussions existentes, sem reiniciar a aplicação ou alterar o modelo global
do OpenCode. Respostas em andamento não são interrompidas. Sem uma escolha salva,
vale `[opencode] model` ou, se omitido, o padrão do OpenCode. Se o catálogo estiver
indisponível, Settings mostra o erro e mantém a escolha anterior.

### OpenCode V2

Configure e autentique um provedor e escolha um modelo padrão no OpenCode antes
de usar o Trainer. A aplicação usa a API HTTP V2, não a API da série 1.x.
O mínimo 2.2.3 garante as políticas que bloqueiam plugins, skills e MCPs externos;
somente as três ferramentas de xadrez do Trainer são permitidas.
Não é necessário reconfigurar `[opencode]` nem migrar o banco da aplicação:
sessões antigas ou incompatíveis são reconstruídas a partir da Discussion salva.
O servidor dedicado usa senha efêmera e não reinicia nem reutiliza o serviço
compartilhado do OpenCode.

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

A verificação desktop em Chromium usa respostas controladas, sem modelo externo.
Veja [tests/browser/README.md](tests/browser/README.md) para instalar o harness e
executar os testes reais de geometria, rolagem e alternância de abas.
