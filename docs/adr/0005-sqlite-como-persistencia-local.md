# SQLite como persistência local

Reafirmamos o SQLite escolhido na [ADR 0002](0002-stack-python-fastapi-htmx.md) porque o Personal Chess Trainer é uma aplicação local, usada por um único Player e executada por um único processo. PostgreSQL acrescentaria servidor, credenciais e operação sem resolver um problema atual; se o produto se tornar multiusuário ou distribuído, outro adapter de persistência poderá ser criado através da fronteira definida na [ADR 0001](0001-arquitetura-hexagonal.md).

## Consequences

- As conexões usam WAL, `busy_timeout` e transações curtas para permitir leituras durante uma escrita e tolerar contenção transitória.
- Cabeçalhos PGN são preservados integralmente como JSON; dados usados em ordenação e filtros previsíveis também têm colunas próprias. Novos índices surgem junto aos filtros concretos.
- Não restringimos o schema ao subconjunto comum entre SQLite e PostgreSQL. Uma futura mudança de banco terá adapter, schema, migrations e transferência de dados próprios.
