# Dados brutos da Game Source como source of truth; Analysis é derivada

Persistimos cada Game como o documento bruto exportado pela Game Source — no Lichess, a exportação **NDJSON** com `evals=true` e `clocks=true`, que traz por lance: eval, mate, melhor lance (`best`, UCI) e variação principal (`variation`, SAN). O PGN com `evals=true` não inclui o melhor lance — e sem ele o Critical Moment ("lance jogado vs melhor lance") não existe. Cada Analysis é um documento JSON derivado; sem tabelas normalizadas de lances.

A classificação (Inaccuracy/Mistake/Blunder) é **do domínio**: implementamos a conversão de eval em winning chances (win% = 50 + 50×wc) e os thresholds oficiais do Lichess (queda de wc ≥ 0.3 / 0.2 / 0.1, com regras próprias para linhas de mate; fonte: `lila`, `Advice.scala`). Os `judgment`s do NDJSON servem como teste de sanidade, não como verdade importada — assim o adapter Chess.com futuro só precisa entregar evals.

A propriedade deliberada é a **re-derivabilidade**: mudar thresholds ou acrescentar engine local significa re-derivar a Analysis do documento local — sem nova chamada à API e sem migração de schema.
