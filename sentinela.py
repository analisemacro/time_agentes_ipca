# sentinela.py
#
# Sentinela: o verificador barato que roda todo dia e decide se vale acordar o
# time de agentes.
#
# Papel: responder duas perguntas independentes, porque o sistema faz duas coisas
# em momentos diferentes do mês:
#
#   1. FALTA POUCO PARA A DIVULGAÇÃO?  A divulgação do IPCA está a caminho e
#      ainda não há previsão registrada para aquele mês de referência. Está na
#      hora de prever — antes que o número saia, senão não é previsão.
#
#   2. A SÉRIE AVANÇOU?  A fonte publicou um ponto mais novo do que o último que
#      temos guardado. O número previsto agora tem um valor real ao lado, e essa
#      comparação precisa ficar registrada.
#
# As duas são independentes: podem acontecer juntas, separadas ou nenhuma.
#
# CÓDIGO PURO, sem LLM nenhum. É o ponto principal do desenho: a sentinela roda
# todo dia, e a maior parte dos dias a resposta é "nada a fazer". Gastar uma
# chamada de modelo para descobrir isso seria queimar token para ouvir "não".
# Comparar duas datas é comparação de datas. O time de agentes — que custa — só
# acorda quando esta função aqui disser que há motivo.
#
# Custo de uma passada: duas requisições HTTP pequenas (calendário do IBGE e os
# 38 bytes do último ponto do SGS) e duas leituras de arquivo local.
#
# A REGRA QUE MAIS IMPORTA: se a fonte não responder, a resposta é
# "nao_deu_para_verificar" — NUNCA "não tem dado novo". As duas se parecem para
# quem só olha o resultado, e confundi-las é o jeito silencioso de o sistema
# ficar semanas sem perceber que a fonte caiu, achando que está tudo em ordem.

from __future__ import annotations

import json
import os
from datetime import date

from memoria import carregar_pontos
from persistencia import (
    anotar_observacao,
    anotar_rodada,
    limpar_temporarios,
    ultima_data_observada,
)
from registro import registrar_realizado, tem_previsao
from tools.calendario_ibge import CalendarioIndisponivel, proxima_divulgacao_ipca
from tools.ultimo_ponto import FonteIndisponivel, ultima_data_publicada

# Com quantos dias de antecedência a previsão deve ser feita.
#
# 1 = na véspera. A previsão vale mais quanto mais perto da divulgação (usa toda
# a informação disponível), mas precisa sair ANTES do número. Um dia é a margem
# mínima que ainda deixa o time rodar, o crítico reprovar e o previsor tentar de
# novo antes de o IBGE publicar.
DIAS_DE_ANTECEDENCIA = 1

# Os quatro desfechos possíveis de uma passada da sentinela.
PREVER = "hora_de_prever"
CONFERIR = "dado_novo_chegou"
AMBOS = "prever_e_conferir"
NADA = "nada_a_fazer"
INDISPONIVEL = "nao_deu_para_verificar"


def _ultima_data_guardada(indicador: str) -> str | None:
    """Contra qual data comparamos para saber se a série avançou.

    Duas fontes, nesta ordem:

      1. data/estado.json — o que a sentinela ANOTOU na rodada anterior. É a
         memória dela entre execuções, e no GitHub Actions é a única coisa que
         sobrevive à máquina ser destruída (desde que commitada de volta).
      2. data/<indicador>.json — a série coletada, como reserva. Cobre a
         primeira execução, quando ainda não há estado anotado, e o caso de
         alguém apagar o estado sem apagar os dados.

    Ficamos com a MAIS RECENTE das duas. Se a coleta avançou a série mas o
    estado ficou para trás (rodada que morreu entre uma coisa e outra), usar a
    data velha faria a sentinela anunciar "dado novo" para um ponto que já está
    guardado — e reprocessar como se fosse novidade.
    """
    do_estado = ultima_data_observada(indicador)

    pontos = carregar_pontos(indicador)
    da_serie = max(pontos) if pontos else None

    candidatas = [d for d in (do_estado, da_serie) if d]
    return max(candidatas) if candidatas else None


def _verificar_divulgacao(indicador: str, hoje: str) -> dict:
    """Pergunta 1: falta pouco para a divulgação, e ainda não previmos?

    Não levanta exceção: devolve um dict com `verificado` dizendo se deu para
    consultar o calendário. Assim uma falha aqui não impede a pergunta 2 de ser
    respondida — as duas são independentes de propósito.
    """
    try:
        proxima = proxima_divulgacao_ipca(hoje=hoje)
    except CalendarioIndisponivel as erro:  # defesa; a função já trata
        return {"verificado": False, "motivo": str(erro)}

    if proxima["status"] != "ok":
        return {"verificado": False,
                "motivo": proxima.get("motivo", proxima["status"]),
                "status_calendario": proxima["status"]}

    ref = proxima["referencia"]
    if ref is None:
        return {"verificado": False,
                "motivo": ("O calendário não informou o mês de referência da "
                           "próxima divulgação.")}

    dias = proxima["dias_ate"]
    ja_previsto = tem_previsao(ref["ano"], ref["mes"])
    esta_na_janela = dias <= DIAS_DE_ANTECEDENCIA

    return {
        "verificado": True,
        "hora_de_prever": esta_na_janela and not ja_previsto,
        "data_divulgacao": proxima["data_divulgacao"],
        "dias_ate": dias,
        "referencia": ref,
        "ja_previsto": ja_previsto,
        "dentro_da_janela": esta_na_janela,
    }


def _verificar_serie(indicador: str) -> dict:
    """Pergunta 2: a fonte publicou ponto mais novo do que o que guardamos?

    Consulta só a DATA do último ponto (38 bytes), não a série inteira.
    """
    guardada = _ultima_data_guardada(indicador)

    try:
        publicado = ultima_data_publicada(indicador)
    except FonteIndisponivel as erro:
        # O ponto central: fonte muda não é "não avançou".
        return {"verificado": False, "motivo": str(erro),
                "ultima_guardada": guardada}

    avancou = guardada is None or publicado["data"] > guardada

    return {
        "verificado": True,
        "dado_novo": avancou,
        "ultima_publicada": publicado["data"],
        "ultima_guardada": guardada,
        "valor_publicado": publicado["valor"],
    }


def verificar(indicador: str = "ipca", hoje: str | None = None) -> dict:
    """Roda a checagem diária e devolve o que a sentinela encontrou.

    Args:
        indicador: qual indicador vigiar (padrão "ipca").
        hoje: data ISO de referência, para testar cenários sem esperar o
            calendário chegar lá. Padrão: hoje.

    Returns:
        dict serializável com:
          - "acao": um de hora_de_prever / dado_novo_chegou / prever_e_conferir
            / nada_a_fazer / nao_deu_para_verificar
          - "resumo": uma linha em português dizendo o que houve
          - "divulgacao": o resultado da pergunta 1
          - "serie": o resultado da pergunta 2
          - "problemas": lista do que não deu para verificar (vazia se tudo ok)

    Nunca levanta exceção por falha de rede — a falha vira `acao`
    "nao_deu_para_verificar" com o motivo, para o agendador registrar e
    (se quiser) alertar.
    """
    referencia_hoje = hoje or date.today().isoformat()

    divulgacao = _verificar_divulgacao(indicador, referencia_hoje)
    serie = _verificar_serie(indicador)

    problemas: list[str] = []
    if not divulgacao["verificado"]:
        problemas.append(f"calendário de divulgações: {divulgacao['motivo']}")
    if not serie["verificado"]:
        problemas.append(f"série do {indicador.upper()}: {serie['motivo']}")

    prever = divulgacao.get("hora_de_prever", False)
    conferir = serie.get("dado_novo", False)

    # Se NENHUMA das duas perguntas pôde ser respondida, não sabemos de nada.
    # Dizer "nada a fazer" aqui seria mentir com cara de normalidade.
    if not divulgacao["verificado"] and not serie["verificado"]:
        acao = INDISPONIVEL
    elif prever and conferir:
        acao = AMBOS
    elif prever:
        acao = PREVER
    elif conferir:
        acao = CONFERIR
    elif problemas:
        # Uma das duas falhou e a outra não achou nada. Não dá para afirmar
        # "nada a fazer": o que falhou pode ser exatamente o que tinha novidade.
        acao = INDISPONIVEL
    else:
        acao = NADA

    return {
        "acao": acao,
        "resumo": _resumir(acao, divulgacao, serie, problemas, indicador),
        "indicador": indicador,
        "verificado_em": referencia_hoje,
        "divulgacao": divulgacao,
        "serie": serie,
        "problemas": problemas,
    }


def _em_dias(dias: int) -> str:
    """'hoje', 'amanhã' ou 'em N dias' — para o resumo sair legível."""
    if dias <= 0:
        return "hoje"
    if dias == 1:
        return "amanhã"
    return f"em {dias} dias"


def _resumir(acao: str, divulgacao: dict, serie: dict, problemas: list[str],
             indicador: str) -> str:
    """Uma linha em português dizendo o que a sentinela encontrou."""
    if acao == INDISPONIVEL:
        return ("Não deu para verificar: " + "; ".join(problemas)
                + ". Nenhuma conclusão foi tirada — isto NÃO significa "
                  "'sem novidade'.")

    partes: list[str] = []

    if acao in (PREVER, AMBOS):
        ref = divulgacao["referencia"]["rotulo"]
        partes.append(
            f"Hora de prever: o {indicador.upper()} de {ref} será divulgado em "
            f"{divulgacao['data_divulgacao']} "
            f"({_em_dias(divulgacao['dias_ate'])}) e ainda não há previsão "
            f"registrada para esse mês."
        )

    if acao in (CONFERIR, AMBOS):
        guardada = serie["ultima_guardada"] or "nada guardado ainda"
        partes.append(
            f"Dado novo: a fonte já publica {serie['ultima_publicada']} e o "
            f"último ponto guardado é {guardada} — há valor real para comparar "
            f"com o previsto."
        )

    if acao == NADA:
        div = ""
        if divulgacao.get("verificado"):
            if divulgacao.get("ja_previsto"):
                div = (f"a divulgação de {divulgacao['referencia']['rotulo']} é "
                       f"em {divulgacao['data_divulgacao']} e já está prevista")
            else:
                div = (f"faltam {divulgacao['dias_ate']} dias para a divulgação "
                       f"de {divulgacao['referencia']['rotulo']}")
        serie_txt = (f"a série segue em {serie['ultima_publicada']}"
                     if serie.get("verificado") else "")
        partes.append("Nada a fazer: "
                      + " e ".join([p for p in (div, serie_txt) if p]) + ".")

    if problemas:
        partes.append("ATENÇÃO, não deu para verificar: " + "; ".join(problemas))

    return " ".join(partes)


def conferir_e_registrar(indicador: str = "ipca", hoje: str | None = None) -> dict:
    """Roda a checagem e, se chegou dado novo, fecha a conferência no caderno.

    Separada de `verificar()` de propósito: `verificar()` só OLHA e não muda
    nada, o que a torna segura de chamar à vontade. Esta aqui ESCREVE — anota o
    valor real ao lado do que havia sido previsto, para o histórico de acerto, e
    atualiza data/estado.json, a memória da sentinela entre execuções.

    Não faz a coleta completa nem chama o time: só registra o valor que a sonda
    barata já trouxe. Coletar a série e rodar os agentes é trabalho do
    orquestrador, depois.
    """
    # Limpa .tmp órfãos de uma execução anterior que morreu no meio da escrita.
    limpar_temporarios()

    resultado = verificar(indicador, hoje)

    # A rodada fica registrada SEMPRE, inclusive quando não deu para verificar —
    # é assim que se percebe uma fonte fora do ar há dias em vez de um silêncio
    # que parece normalidade.
    anotar_rodada(resultado["acao"], quando=resultado["verificado_em"],
                  detalhe=resultado["resumo"][:200])

    # Anota o que a fonte publicava AGORA, para a próxima rodada ter contra o
    # que comparar. Só quando a consulta deu certo: gravar depois de uma falha
    # de rede sobrescreveria a memória boa com um "não sei".
    if resultado["serie"].get("verificado"):
        anotar_observacao(
            indicador,
            resultado["serie"]["ultima_publicada"],
            valor=resultado["serie"].get("valor_publicado"),
            quando=resultado["verificado_em"],
        )

    if resultado["acao"] not in (CONFERIR, AMBOS):
        resultado["conferencia"] = None
        return resultado

    publicada = date.fromisoformat(resultado["serie"]["ultima_publicada"])
    # observado_em é o dia em que VIMOS o valor na fonte — que é hoje, o dia
    # desta passada da sentinela. É essa data que permite saber depois, se o
    # IBGE revisar o número, contra qual versão o erro foi calculado.
    entrada = registrar_realizado(
        publicada.year, publicada.month,
        resultado["serie"]["valor_publicado"],
        observado_em=resultado["verificado_em"],
    )

    if entrada is None:
        resultado["conferencia"] = {
            "registrado": False,
            "motivo": (f"Chegou o dado de {publicada.isoformat()}, mas não havia "
                       f"previsão registrada para esse mês — nada a comparar."),
        }
    else:
        resultado["conferencia"] = {
            "registrado": True,
            "referencia": entrada["referencia"],
            "previsto": entrada["valor_previsto"],
            "real": entrada["valor_real"],
            "erro": entrada["erro"],
        }

    return resultado


def _publicar_saida_para_o_ci(resultado: dict) -> None:
    """Escreve a decisão em GITHUB_OUTPUT, para o passo seguinte do workflow ler.

    É assim que o GitHub Actions passa valor de um passo para outro: linhas
    `chave=valor` num arquivo cujo caminho vem na variável GITHUB_OUTPUT. Fora do
    Actions a variável não existe e a função não faz nada.

    `rodar_time` é o que interessa ao workflow: "true" só quando há motivo para
    gastar chamadas de LLM.
    """
    caminho = os.environ.get("GITHUB_OUTPUT")
    if not caminho:
        return

    acao = resultado["acao"]
    rodar = acao in (PREVER, CONFERIR, AMBOS)

    # O resumo vai numa linha só: GITHUB_OUTPUT é formato chave=valor, e uma
    # quebra de linha no meio do valor corromperia o arquivo.
    resumo = " ".join(resultado["resumo"].split())

    with open(caminho, "a", encoding="utf-8") as saida:
        saida.write(f"acao={acao}\n")
        saida.write(f"rodar_time={'true' if rodar else 'false'}\n")
        saida.write(f"resumo={resumo}\n")


if __name__ == "__main__":
    import sys

    # A data pode vir como argumento (para testar um cenário); sem ela, é hoje.
    hoje = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "--ci" else None

    # No CI a sentinela ESCREVE (anota o que viu, para a rodada seguinte ter
    # memória). Rodando na mão, ela só olha — chamar `verificar` à toa não pode
    # mexer no estado.
    modo_ci = "--ci" in sys.argv
    resultado = (conferir_e_registrar(hoje=hoje) if modo_ci
                 else verificar(hoje=hoje))

    print("=" * 72)
    print(f"SENTINELA — {resultado['verificado_em']}")
    print("=" * 72)
    print(f"AÇÃO: {resultado['acao']}")
    print(f"\n{resultado['resumo']}\n")

    if modo_ci:
        _publicar_saida_para_o_ci(resultado)
        # Uma fonte fora do ar não é "nada a fazer": o job deve FALHAR para
        # aparecer vermelho no painel do Actions. Silenciar aqui é como o
        # sistema fica semanas quebrado sem ninguém notar.
        if resultado["acao"] == INDISPONIVEL:
            print("::error::A sentinela não conseguiu verificar as fontes.")
            for problema in resultado["problemas"]:
                print(f"::error::{problema}")
            raise SystemExit(1)
    else:
        print("-" * 72)
        print(json.dumps(resultado, ensure_ascii=False, indent=2))
