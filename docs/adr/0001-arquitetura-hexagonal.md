# Arquitetura Hexagonal (ports & adapters)

O projeto tem múltiplas fronteiras externas — Game Sources trocáveis (Lichess hoje, Chess.com anunciado para o futuro), persistência local, e a interface HTTP entre backend e frontend. Decidimos organizar o backend em Arquitetura Hexagonal: o domínio (análise das partidas do Player) fica isolado no centro e só interage com o exterior através de portas que ele mesmo define; cada provedor externo é um adapter de uma porta.

## Consequences

- Chess.com entrará como um novo adapter da mesma porta de Game Source, sem tocar o domínio.
- Cada nova fronteira (engine de avaliação, export de relatórios) nasce como porta definida pelo domínio, não como dependência importada por ele.
