# Histórico de alterações

## Não lançado

- Proposta de documentação técnica, auditoria de base e critérios de revisão por pull request.
- Seleção de moedas corrigida: usa só pools em alta, descarta valor de mercado acima de US$ 2 bi e, depois de baixar os candles, descarta pools pouco voláteis (BTC, ETH, stablecoins). Mantém as 5 mais ativas.
