# Discussions e Variations persistidas

O Trainer precisa retomar o raciocínio construído com o Player e comparar linhas alternativas sem perder explorações anteriores. Por isso, cada Base Position pode ter uma Discussion persistida, cujas mensagens e árvores de Variations sobrevivem ao reinício do aplicativo; esta decisão revisa a consequência da ADR 0002 que mantinha variações exploratórias somente no cliente e sem persistência.

## Consequences

- A Base Position é identificada pela Game e pelo ply, não apenas pelo FEN.
- Movimentos de Player e Trainer são registrados com autoria e Position resultante.
- Games e Analyses continuam imutáveis; Variations pertencem exclusivamente à Discussion.
