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
