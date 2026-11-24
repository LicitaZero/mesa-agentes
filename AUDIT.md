# Auditoria técnica da Mesa de Agentes

Base auditada: `34cb059cad316308c8c994db1f5b4ec81dba65e2`. Revisão estática do código e do workflow.

| Prioridade | Achado e evidência | Critério de correção |
|---|---|---|
| Alta | O workflow de produção concentra o processamento, a publicação e a escrita de estado. `.github/workflows/mesa.yml` configura um job de até 355 minutos e atualiza as branches de saída. | Separar permissões por etapa e preservar estado em falha ou concorrência. |
| Alta | A interface do relatório incorpora dados externos. `mesa_agentes.py`, funções de geração e renderização. | Revisar renderização segura e criar testes de regressão com dados sintéticos. |
| Alta | A simulação assume ordem intrabar a partir de OHLC e seleciona pools com dados de mercado atuais. `mesa_agentes.py`, funções `cmd_descobrir` e `simular`. | Documentar hipóteses, separar descoberta de avaliação temporal e verificar vieses com casos controlados. |
| Média | A recuperação de JSON usa fallback genérico. `mesa_agentes.py`, função `ler_json`. | Distinguir ausência, corrupção e falha de leitura, preservando o último estado válido. |
| Média | O repositório tem um script principal, README mínimo e workflow operacional. A base não tem suíte de testes. | Adicionar cenários determinísticos de simulador e smoke test de relatório em PR. |
| Média | O loop contínuo consome capacidade de execução e atualiza repetidamente as branches de dados. | Medir minutos, duração, frequência de chamadas e crescimento do histórico antes de alterar cadência. |

A Mesa é um simulador de mercado escrito em Python, com estado em JSON e publicação por GitHub Pages. Não foi realizada revisão completa do histórico de credenciais ou do ambiente de produção. Resultados da simulação dependem da qualidade e da cobertura das fontes e das premissas documentadas.
