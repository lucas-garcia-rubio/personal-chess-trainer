# Flyway para migrations do schema

Usaremos Flyway para evoluir o schema SQLite decidido na [ADR 0005](0005-sqlite-como-persistencia-local.md). Migrations SQL explícitas e versionadas tornam o histórico fácil de inspecionar, validar e, se outro banco for adotado, transcrever conscientemente; Flyway padroniza o processo, mas não torna SQL SQLite portável para PostgreSQL nem transfere dados automaticamente.

## Consequences

- `trainer serve` executa `flyway validate` e `flyway migrate` antes de abrir o storage e iniciar o servidor; uma falha interrompe o startup.
- O caminho do banco continua definido apenas em `config.toml`. A aplicação fornece ao Flyway a URL JDBC absoluta e a localização das migrations.
- O executável pode ser definido em `[migration] flyway_path` ou encontrado como `flyway` no `PATH`, e sua versão suportada é verificada. Futuramente, a imagem Docker ou seu script de inicialização poderá fornecê-lo.
- Migrations aplicadas são imutáveis. Correções entram em uma nova versão e a validação de checksum detecta alterações retroativas.
- Um banco legado só recebe baseline depois que seu schema conhecido for reconhecido estritamente; `baselineOnMigrate` não fica habilitado indiscriminadamente. Bancos vazios executam todo o histórico.
- Mudanças de schema e backfills, inclusive cabeçalhos e proveniência de Analyses existentes, permanecem visíveis nas migrations versionadas.
