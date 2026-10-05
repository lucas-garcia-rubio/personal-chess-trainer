# Stack: Python + FastAPI + python-chess + htmx

Decidimos construir o backend em Python (FastAPI) com `python-chess`, frontend server-rendered via htmx + Jinja e persistência em SQLite. O fator decisivo foi o domínio: manipular lances, posições, PGN e seus adornos (`%eval`, `%clk`, variações) — o insumo de toda Analysis — é trivial em `python-chess` e frágil nas alternativas JS.

## Considered Options

- **Rust (axum + shakmaty)** — `shakmaty` é excelente; rejeitado apenas pela velocidade de iteração de uma v1 pessoal, não por capacidade. Pode voltar se performance virar questão.
- **Node/TS full-stack** — rejeitado: `chess.js` não lida bem com PGN anotado.

## Consequences

- Sem SPA: relatório e navegação são server-rendered. O único JS além do próprio htmx é a ilha do tabuleiro: **chessground** (a lib de tabuleiro do próprio Lichess), zero-dependência e framework-agnóstica.
- O tabuleiro é interativo desde a v1 — explorar posições livremente, sem persistir as variações exploradas — porque o produto evoluirá para um Trainer em tempo real (propostas de linha, continuação pelo Player, comentários ao vivo). A ilha mantém o FEN no cliente; o servidor continua sendo a fonte da verdade das Games e Analyses.
