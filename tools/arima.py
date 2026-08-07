# tools/arima.py
#
# Tool: previsão de um passo à frente com ARIMA (statsmodels).
#
# Papel: ser a CALCULADORA do Agente Previsor. O LLM decide RODAR esta função e
# o que fazer com o resultado; o número previsto e o intervalo saem daqui, do
# modelo estatístico — nunca da cabeça do modelo de linguagem.
#
# Mesma filosofia da coleta: preferir FALHAR a entregar um número ruim. Série
# curta demais não vira "previsão fraquinha", vira recusa explícita com o motivo.

from __future__ import annotations

import warnings

from langchain_core.tools import tool

# Mínimo de observações para levar a sério um ARIMA em série MENSAL.
#
# Por que 24: o modelo precisa estimar os parâmetros (p, d, q) e ainda sobrar
# grau de liberdade. Com série mensal, 24 pontos são dois ciclos anuais — o piso
# para o modelo enxergar repetição em vez de ruído. Abaixo disso o intervalo de
# confiança fica largo a ponto de não informar nada, e o ponto central vira
# quase o valor do último mês.
#
# Não é um número sagrado; é um piso conservador. Se mudar, mude com um motivo.
MINIMO_OBSERVACOES = 24

# Ordem padrão (p, d, q) para a variação mensal do IPCA.
#
# d=0 de propósito: a série JÁ É uma variação percentual (não o índice de
# preços), então não precisa ser diferenciada de novo — diferenciar uma taxa
# costuma introduzir ruído em vez de remover tendência.
ORDEM_PADRAO = (1, 0, 1)


def _serie_de(pontos: list[dict]) -> tuple[list[str], list[float]]:
    """Separa os pontos em (datas, valores), ordenados por data.

    Aceita os pontos no formato em que o coletor os deixa no estado e em
    data/<indicador>.json: [{"data": "AAAA-MM-DD", "valor": 0.33}, ...].
    """
    limpos = [
        (str(p["data"]), float(p["valor"]))
        for p in pontos
        if p.get("data") is not None and p.get("valor") is not None
    ]
    limpos.sort(key=lambda dv: dv[0])
    return [d for d, _ in limpos], [v for _, v in limpos]


@tool
def prever_arima(
    pontos: list[dict],
    ordem_p: int = ORDEM_PADRAO[0],
    ordem_d: int = ORDEM_PADRAO[1],
    ordem_q: int = ORDEM_PADRAO[2],
    nivel_confianca: float = 0.95,
) -> dict:
    """Prevê o PRÓXIMO valor de uma série mensal com um modelo ARIMA.

    Use esta ferramenta para calcular a previsão. O número previsto e o intervalo
    saem do modelo estatístico (statsmodels) — você NÃO deve estimar o valor por
    conta própria nem "ajustar no olho" o que a ferramenta devolver.

    A ferramenta RECUSA prever quando a série é curta demais (menos de 24
    observações mensais): nesse caso devolve {"status": "historico_insuficiente"}
    com o que falta. Recusa é o resultado correto, não um erro a contornar —
    relate a falta de histórico em vez de arriscar um número ruim.

    Se o crítico reprovou uma previsão anterior, você pode variar a ordem do
    modelo (ordem_p, ordem_d, ordem_q) e rodar de novo. Use isso para responder
    ao motivo da reprovação; não repita a mesma chamada esperando outro número —
    o modelo é determinístico e devolveria exatamente o mesmo resultado.

    Args:
        pontos: a série já coletada e validada, como
            [{"data": "AAAA-MM-DD", "valor": 0.33}, ...].
        ordem_p: termo autorregressivo (AR). Padrão 1.
        ordem_d: diferenciação (I). Padrão 0 — a série já é uma variação %.
        ordem_q: termo de média móvel (MA). Padrão 1.
        nivel_confianca: nível do intervalo, entre 0 e 1. Padrão 0.95 (95%).

    Returns:
        Em caso de sucesso:
        {"status": "ok", "valor": 0.42, "intervalo": {"minimo": 0.1,
         "maximo": 0.74, "nivel": 0.95}, "modelo": "ARIMA(1,0,1)",
         "observacoes": 36, "ultima_data": "2026-06-01"}

        Se faltar histórico:
        {"status": "historico_insuficiente", "observacoes": 6,
         "minimo_exigido": 24, "faltam": 18, "motivo": "..."}

        Se o modelo não convergir:
        {"status": "falha_no_modelo", "motivo": "..."}
    """
    datas, valores = _serie_de(pontos)
    n = len(valores)

    # GUARDRAIL 1 — histórico curto. Recusa explícita, com o que falta.
    if n < MINIMO_OBSERVACOES:
        return {
            "status": "historico_insuficiente",
            "observacoes": n,
            "minimo_exigido": MINIMO_OBSERVACOES,
            "faltam": MINIMO_OBSERVACOES - n,
            "motivo": (
                f"a série tem {n} observação(ões) e o mínimo para um ARIMA "
                f"mensal confiável é {MINIMO_OBSERVACOES} (dois ciclos anuais). "
                f"Faltam {MINIMO_OBSERVACOES - n}. Prever com menos que isso "
                f"produziria um intervalo largo demais para informar qualquer "
                f"coisa — colete mais histórico antes de prever."
            ),
        }

    if not 0 < nivel_confianca < 1:
        return {
            "status": "falha_no_modelo",
            "motivo": f"nivel_confianca deve estar entre 0 e 1 (recebido: {nivel_confianca})",
        }

    # GUARDRAIL 2 — o modelo pode não convergir. Falha vira status, não exceção
    # solta: o agente precisa conseguir LER o problema e reagir.
    try:
        from statsmodels.tsa.arima.model import ARIMA

        ordem = (int(ordem_p), int(ordem_d), int(ordem_q))
        with warnings.catch_warnings():
            # statsmodels avisa sobre datas sem frequência declarada; a série é
            # mensal por construção e o aviso poluiria a saída da aula.
            warnings.simplefilter("ignore")
            ajuste = ARIMA(valores, order=ordem).fit()
            previsao = ajuste.get_forecast(steps=1)

            valor = float(previsao.predicted_mean[0])
            # alpha = 1 - nível: 0.05 para um intervalo de 95%.
            faixa = previsao.conf_int(alpha=1 - nivel_confianca)
            minimo, maximo = float(faixa[0][0]), float(faixa[0][1])
    except Exception as e:
        return {
            "status": "falha_no_modelo",
            "motivo": f"o ARIMA não convergiu ou falhou ao estimar: {e}",
        }

    return {
        "status": "ok",
        "valor": round(valor, 4),
        "intervalo": {
            "minimo": round(minimo, 4),
            "maximo": round(maximo, 4),
            "nivel": nivel_confianca,
        },
        "modelo": f"ARIMA({ordem[0]},{ordem[1]},{ordem[2]})",
        "observacoes": n,
        "ultima_data": datas[-1] if datas else None,
    }
