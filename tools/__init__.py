# tools/__init__.py
#
# Pacote de tools do Agente de Coleta.
#
# Papel: reunir as funções determinísticas (código puro) que os agentes expõem ao
# Gemini como ferramentas — coleta em BCB/SGS e IBGE/SIDRA, e o cálculo da
# previsão com ARIMA. Cada tool devolve número REAL (da fonte oficial ou do
# modelo estatístico); o LLM apenas as orquestra.

from tools.arima import prever_arima
from tools.bcb_sgs import coletar_serie_sgs
from tools.calendario_ibge import (
    CalendarioIndisponivel,
    divulgacoes_ipca,
    proxima_divulgacao_ipca,
)
from tools.diagnostico import diagnosticar
from tools.ibge_sidra import coletar_sidra, descrever_tabela_sidra

# Tools do Agente de Coleta (coletor.py). Mantida com este nome e este conteúdo
# para não mudar o que o coletor já enxerga.
TOOLS = [
    coletar_serie_sgs,
    coletar_sidra,
    descrever_tabela_sidra,
]

# Tools do Agente Previsor (previsor.py) — a calculadora do time.
TOOLS_PREVISAO = [
    prever_arima,
]

# `diagnosticar` NÃO entra em nenhuma lista de tools: o crítico não a chama como
# ferramenta, o código roda antes e entrega o resultado pronto no pedido. Fica
# exportada aqui só para quem quiser usá-la direto.
#
# O calendário do IBGE (`proxima_divulgacao_ipca`, `divulgacoes_ipca`) também
# NÃO entra em lista de tool nenhuma, e pelo mesmo motivo: quem lê o calendário
# é a sentinela, em código puro, antes de o grafo começar. Consultar uma data
# publicada não exige julgamento — não é trabalho de agente.

__all__ = [
    "TOOLS",
    "TOOLS_PREVISAO",
    "CalendarioIndisponivel",
    "coletar_serie_sgs",
    "coletar_sidra",
    "descrever_tabela_sidra",
    "diagnosticar",
    "divulgacoes_ipca",
    "prever_arima",
    "proxima_divulgacao_ipca",
]
