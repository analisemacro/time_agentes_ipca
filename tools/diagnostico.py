# tools/diagnostico.py
#
# Evidências para o Agente Crítico. Código puro, sem LLM e sem rede.
#
# Papel: medir a previsão contra a série que já está no mural e devolver os
# NÚMEROS que sustentam (ou derrubam) a previsão. O crítico não busca nada nem
# calcula de cabeça: ele recebe estas medidas prontas e JULGA.
#
# Se o LLM tivesse que estimar "o salto é grande?" no olho, estaríamos de volta ao
# chute que o projeto inteiro evita. Aqui a aritmética é determinística; o que fica
# para o agente é decidir se aquilo é aceitável.
#
# Esta função NÃO decide aprovar ou reprovar — ela só mede. O veredito é do
# crítico, que pode ponderar as evidências e até discordar de um sinal isolado.

from __future__ import annotations

import statistics

# Quantos meses contam como "tendência recente" ao comparar o salto.
JANELA_RECENTE = 6

# Referências para o julgamento do salto, em desvios-padrão da série recente.
# Não são vereditos automáticos: entram no relatório como leitura sugerida e o
# crítico decide o peso.
SALTO_ATENCAO = 2.0   # acima disso, merece explicação
SALTO_GRAVE = 3.0     # acima disso, é quase certo que algo está errado

# Referências para a largura do intervalo de confiança, em PONTOS PERCENTUAIS
# da variação mensal do IPCA.
#
# Por que em p.p. e não como múltiplo do desvio da série: o IC de um passo à
# frente é naturalmente da ordem de vários desvios do resíduo, então comparar
# com o desvio da série reprovaria qualquer previsão boa. O que interessa é se a
# faixa é utilizável para quem lê: dizer "o IPCA de julho fica entre 0,1% e
# 1,5%" não ajuda ninguém a decidir nada.
#
# Calibragem para o IPCA mensal, que costuma rodar entre 0% e 1% ao mês:
IC_LARGO_PP = 1.2     # faixa maior que 1,2 p.p. cobre quase todo o range plausível
IC_ESTREITO_PP = 0.08  # faixa menor que 0,08 p.p. é precisão que ninguém tem

# Piso de segurança: um IC praticamente nulo é degenerado, não "preciso".
IC_DEGENERADO_PP = 0.01


def _valores(serie: list[dict]) -> list[float]:
    """Extrai os valores da série, ordenados por data."""
    limpos = [
        (str(p["data"]), float(p["valor"]))
        for p in serie
        if p.get("data") is not None and p.get("valor") is not None
    ]
    limpos.sort(key=lambda dv: dv[0])
    return [v for _, v in limpos]


def diagnosticar(serie: list[dict], previsao: dict) -> dict:
    """Mede a previsão contra a série e devolve as evidências.

    Args:
        serie: a série validada, [{"data": "AAAA-MM-DD", "valor": 0.33}, ...].
        previsao: o dict que o previsor deixou no mural (com "valor",
            "intervalo", "modelo", "observacoes").

    Returns:
        dict com as medidas e, em "sinais", a lista de alertas encontrados. Lista
        vazia em "sinais" significa que nenhuma das checagens automáticas
        disparou — NÃO significa "aprovado", que é decisão do crítico.
    """
    valores = _valores(serie)
    n = len(valores)

    if previsao.get("status") != "ok" or previsao.get("valor") is None:
        return {
            "avaliavel": False,
            "motivo": (
                f"não há previsão numérica para avaliar "
                f"(status: {previsao.get('status', 'ausente')})"
            ),
        }

    if n < 2:
        return {
            "avaliavel": False,
            "motivo": f"a série tem {n} observação(ões) — poucas para comparar.",
        }

    valor = float(previsao["valor"])
    recentes = valores[-JANELA_RECENTE:]
    ultimo = valores[-1]
    media_recente = statistics.fmean(recentes)
    # Desvio da janela recente. Com menos de 2 pontos na janela não há desvio;
    # com desvio zero (série constante) evitamos divisão por zero mais abaixo.
    desvio_recente = statistics.stdev(recentes) if len(recentes) >= 2 else 0.0

    diagnostico: dict = {
        "avaliavel": True,
        "observacoes": n,
        "ultimo_valor": round(ultimo, 4),
        "media_recente": round(media_recente, 4),
        "desvio_recente": round(desvio_recente, 4),
        "janela_recente": min(JANELA_RECENTE, n),
        "valor_previsto": round(valor, 4),
        "salto_vs_ultimo": round(valor - ultimo, 4),
        "salto_vs_media": round(valor - media_recente, 4),
    }

    sinais: list[str] = []

    # --- 1) O salto é estranho para a tendência recente? ---
    if desvio_recente > 0:
        desvios = abs(valor - media_recente) / desvio_recente
        diagnostico["salto_em_desvios"] = round(desvios, 2)
        direcao = "acima" if valor > media_recente else "abaixo"
        if desvios >= SALTO_GRAVE:
            sinais.append(
                f"SALTO GRAVE: a previsão ({valor}) está {desvios:.1f} desvios-padrão "
                f"{direcao} da média dos últimos {len(recentes)} meses "
                f"({media_recente:.2f}). Um pulo desse tamanho precisa de uma razão "
                f"econômica explícita — sem ela, é mais provável que o modelo esteja errado."
            )
        elif desvios >= SALTO_ATENCAO:
            sinais.append(
                f"SALTO SUSPEITO: a previsão ({valor}) está {desvios:.1f} desvios-padrão "
                f"{direcao} da média recente ({media_recente:.2f}). Merece explicação."
            )
    else:
        diagnostico["salto_em_desvios"] = None
        sinais.append(
            "série recente sem variação (desvio zero) — não dá para julgar o salto "
            "estatisticamente; desconfie da própria série."
        )

    # --- 2) O intervalo é largo demais ou estreito demais? ---
    intervalo = previsao.get("intervalo") or {}
    minimo, maximo = intervalo.get("minimo"), intervalo.get("maximo")
    if minimo is not None and maximo is not None:
        largura = float(maximo) - float(minimo)
        diagnostico["largura_intervalo"] = round(largura, 4)

        # O intervalo tem de conter o próprio ponto previsto. Se não contém, o
        # resultado está internamente inconsistente — erro, não questão de grau.
        if not (float(minimo) <= valor <= float(maximo)):
            sinais.append(
                f"INCONSISTÊNCIA: o valor previsto ({valor}) está FORA do próprio "
                f"intervalo [{minimo}, {maximo}]. Isso é um erro de cálculo, não "
                f"uma questão de opinião."
            )

        if largura <= IC_DEGENERADO_PP:
            sinais.append(
                f"INTERVALO DEGENERADO: a faixa [{minimo}, {maximo}] tem largura "
                f"{largura:.3f} p.p., praticamente zero. Isso não é precisão, é sinal "
                f"de que a estimativa da incerteza falhou."
            )
        elif largura >= IC_LARGO_PP:
            sinais.append(
                f"INTERVALO LARGO DEMAIS: a faixa [{minimo}, {maximo}] tem largura "
                f"{largura:.2f} p.p. Para o IPCA mensal, que costuma rodar entre 0% e "
                f"1%, uma faixa dessas aceita quase qualquer desfecho e não ajuda a "
                f"decidir nada — tente uma ordem mais simples."
            )
        elif largura <= IC_ESTREITO_PP:
            sinais.append(
                f"INTERVALO ESTREITO DEMAIS: a faixa [{minimo}, {maximo}] tem largura "
                f"{largura:.3f} p.p. Prever o IPCA mensal com essa precisão não é "
                f"realista — provável subestimação da incerteza."
            )

        # Contexto útil para o crítico ponderar, sem virar alerta por si só.
        if desvio_recente > 0:
            diagnostico["largura_em_desvios"] = round(largura / desvio_recente, 2)
    else:
        diagnostico["largura_intervalo"] = None
        sinais.append("a previsão não trouxe intervalo de confiança — sem isso não há "
                      "como julgar a incerteza.")

    # --- 3) O modelo forçou o ajuste numa série curta? ---
    modelo = str(previsao.get("modelo", ""))
    parametros = _contar_parametros(modelo)
    obs_modelo = int(previsao.get("observacoes", n) or n)
    diagnostico["parametros_do_modelo"] = parametros
    if parametros:
        # Regra prática: ao menos ~10 observações por parâmetro estimado. Abaixo
        # disso o modelo tende a decorar o ruído da amostra em vez de aprender.
        obs_por_parametro = obs_modelo / parametros
        diagnostico["observacoes_por_parametro"] = round(obs_por_parametro, 1)
        if obs_por_parametro < 10:
            sinais.append(
                f"AJUSTE FORÇADO: {modelo} estima {parametros} parâmetro(s) com apenas "
                f"{obs_modelo} observações ({obs_por_parametro:.1f} por parâmetro). "
                f"Abaixo de ~10 por parâmetro o modelo tende a decorar o ruído da "
                f"amostra — considere uma ordem mais simples."
            )

    diagnostico["sinais"] = sinais
    return diagnostico


def _contar_parametros(modelo: str) -> int:
    """Conta os parâmetros estimados a partir do rótulo 'ARIMA(p,d,q)'.

    Soma p + q (os termos estimados); d é diferenciação, não parâmetro estimado.
    Devolve 0 se o rótulo não for reconhecido — nesse caso a checagem 3 não roda.
    """
    try:
        dentro = modelo[modelo.index("(") + 1:modelo.index(")")]
        p, d, q = (int(x.strip()) for x in dentro.split(","))
        return p + q
    except (ValueError, IndexError):
        return 0
