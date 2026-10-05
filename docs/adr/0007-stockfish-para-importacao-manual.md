# Stockfish para Import de PGN

Introduzimos Import como alternativa manual ao Sync: o Player fornece exatamente uma Game em PGN, e o Stockfish local avalia sua linha principal antes que Game e Analysis sejam persistidas atomicamente. Esta decisão revisa o alcance da [ADR 0003](0003-evals-lichess-sem-engine-local.md): o Sync continua usando avaliações do servidor do Lichess, enquanto o novo Import usa a engine local através da porta de avaliação prevista naquela ADR e na [ADR 0001](0001-arquitetura-hexagonal.md).

O PGN recebido permanece bruto e imutável como source of truth, ampliando a decisão da [ADR 0004](0004-pgn-bruto-como-source-of-truth.md). Avaliações, melhores lances, principal variação, nome e versão da engine e parâmetros usados pertencem ao documento derivado da Analysis; comentários e variações do PGN são preservados, mas apenas a linha principal é analisada.

## Consequences

- A configuração inicial usa profundidade 15, uma thread e 128 MB de hash, todos configuráveis. Cada Import reutiliza uma instância do Stockfish durante a Game e sempre a encerra ao terminar.
- O Import é síncrono, limitado a 1.000 plies e a dez minutos por padrão. Qualquer falha de validação ou avaliação impede toda a persistência.
- O executável pode ser definido em `[engine] path` ou encontrado como `stockfish` no `PATH`. Sua ausência não impede o Sync, mas torna o Import indisponível com erro explícito.
- Games mantêm identidade por origem. Um PGN do Lichess com `GameId` pode reconhecer uma Game já sincronizada; nesse caso, o Import abre a Analysis existente em vez de recalculá-la silenciosamente.
