# Evals do Lichess como avaliações da v1 — sem engine local

A v1 usa as avaliações da análise de servidor do Lichess que já vêm na exportação da Game Source, sem rodar engine local (zero instalação, Analysis em segundos). A avaliação de posições fica atrás de uma porta `PositionEvaluator` definida pelo domínio — Stockfish local entra depois como adapter alternativo (ou de segunda opinião) sem tocar o domínio.

## Consequences

- A qualidade da Analysis na v1 é limitada pela profundidade da análise de servidor do Lichess.
- Reanalisar uma Game com engine local no futuro exige apenas um novo adapter e um comando de re-análise.
