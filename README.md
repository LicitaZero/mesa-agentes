# mesa-agentes

## Desenvolvimento e operação

A Mesa coleta dados de mercado e gera relatórios de simulação. Execute `python mesa_agentes.py --help` para ver os modos e `python mesa_agentes.py demo --help` para configurar a demonstração. O workflow existente publica o relatório e mantém o estado da operação.

Consulte [requisitos](REQUIREMENTS.md), [auditoria](AUDIT.md), [roadmap](ROADMAP.md) e [instruções para agentes](AGENTS.md). Abra uma issue com critérios de aceitação antes de propor mudanças em um pull request.

## Paper trading online

- Acesse o painel em https://licitazero.github.io/mesa-agentes/.
- Para iniciar ou testar uma rodada pelo GitHub, abra **Actions → mesa-agentes → Run workflow** na branch **main**.
- O GitHub agenda rodadas a cada cinco minutos e pode atrasar. O painel verifica os dados a cada minuto. Atrasos podem impedir o registro de sinais ao vivo.
- A campanha usa até cinco pools por liquidez e volume, com exclusão de SOL e stablecoins conhecidas. A identificação por pool e símbolo pode incluir outros tokens especulativos. As fontes são GeckoTerminal e RugCheck.
- A branch de dados vivo guarda estado, candles, log e resultado final. O motor encerra na primeira rodada após o fim de 31/12/2026 em Brasília, com o último candle fechado disponível. Rodadas posteriores preservam esse resultado.
- A operação é inteiramente simulada. O histórico de risco não é retroativo. O Rug Gate ao vivo registra parecer por candle quando observado, e o backtest padrão mantém a ausência dessa fonte explícita.
