# OpenCode como runtime do Trainer

O Trainer usará um servidor headless do OpenCode para aproveitar o modelo e a autenticação já configurados pelo Player. Cada Discussion mantém o ID de uma sessão OpenCode, enquanto o SQLite continua sendo a fonte da verdade e permite reconstruir uma sessão perdida; a aplicação não integra diretamente SDKs de provedores de LLM.

O OpenCode acessa movimentos, avaliações e, futuramente, a Chess Library por um MCP exposto pelo Personal Chess Trainer. Um agente `trainer` dedicado pode usar somente essas ferramentas aprovadas, sem acesso a shell, arquivos, ferramentas de programação ou outros MCPs.

## Consequences

- O Personal Chess Trainer consome a API e o stream de eventos do `opencode serve`.
- A aplicação inicia o servidor em `127.0.0.1` como processo filho e o encerra no shutdown.
- As sessões executam em um diretório privado com configuração e agente próprios, separados do repositório e de ferramentas de desenvolvimento.
- A escolha e a autenticação do modelo permanecem na configuração padrão do OpenCode.
- Chamadas MCP passam pelos casos de uso da aplicação, que validam e persistem toda mudança antes de publicá-la ao navegador.
- Uma sessão nova recebe o Discussion Context completo; sessões existentes recebem somente eventos posteriores e a Position ativa.
- O MCP permite reler o Discussion Context por revisão; ferramentas que recebem uma revisão obsoleta não podem alterar a árvore de Variations.
- OpenCode e MCP usam credenciais efêmeras por execução que não chegam ao navegador nem ao banco.
- Somente conteúdo visível ao Player é persistido; eventos técnicos e raciocínio interno do OpenCode não pertencem à Discussion.
- A indisponibilidade do OpenCode impede novas respostas do Trainer, mas não compromete Discussions já persistidas nem as demais capacidades da aplicação.
