# time.py
#
# Supervisor do time de agentes — o grafo que amarra os quatro na ordem certa.
#
# Papel: rodar coleta -> previsão -> crítica -> redação a partir de um pedido em
# linguagem natural, com um laço de correção entre previsor e crítico.
#
# O supervisor é CÓDIGO PURO: ele não é um LLM decidindo o que vem depois. Quem
# decide o fluxo é a aresta condicional abaixo, olhando o parecer e a contagem de
# tentativas. Agente é para julgamento; ordenar quatro passos conhecidos é
# trabalho de código.
#
# Grafo:
#
#   coleta ──> previsao ──> critica ──(aprovou? OU estourou o teto?)──> redacao ──> FIM
#                  ▲                          │
#                  └──────(reprovou)──────────┘
#
# O laço não é infinito: TETO_TENTATIVAS limita as rodadas. Ao estourar, o
# relatório sai mesmo assim — com a ressalva da reprovação bem visível, que é o
# ponto do sistema inteiro.

from __future__ import annotations

import json

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from coletor import _DB_CHECKPOINTS, no_agente, no_ferramentas, no_guardar
from critico import no_critico, no_registrar_parecer
from previsor import no_ferramentas_previsao, no_previsor, no_registrar_previsao
from redator import no_redator, no_registrar_relatorio
from state import EstadoColeta

# Quantas vezes o previsor pode tentar até o crítico aprovar.
#
# 3 é um meio-termo: dá espaço para o previsor reagir a dois pareceres
# diferentes, e para de queimar chamada de LLM quando fica claro que o modelo não
# vai convergir para algo que o crítico aceite. Ao estourar, o relatório sai com
# a ressalva — publicar "não aprovado, e por isto" é mais útil que não publicar.
TETO_TENTATIVAS = 3


# --- Subgrafo da coleta -------------------------------------------------------
# Reaproveita os nós do coletor.py. Como o coletor tem seu próprio laço interno
# (agente <-> ferramentas), ele entra como subgrafo, não como nó solto.
def _subgrafo_coleta():
    from langgraph.prebuilt import tools_condition

    g = StateGraph(EstadoColeta)
    g.add_node("agente", no_agente)
    g.add_node("ferramentas", no_ferramentas)
    g.add_node("guardar", no_guardar)
    g.add_edge(START, "agente")
    g.add_conditional_edges(
        "agente", tools_condition, {"tools": "ferramentas", END: "guardar"}
    )
    g.add_edge("ferramentas", "agente")
    g.add_edge("guardar", END)
    return g.compile()


def _subgrafo_previsao():
    from langgraph.prebuilt import tools_condition

    g = StateGraph(EstadoColeta)
    g.add_node("previsor", no_previsor)
    g.add_node("ferramentas_previsao", no_ferramentas_previsao)
    g.add_node("registrar_previsao", no_registrar_previsao)
    g.add_edge(START, "previsor")
    g.add_conditional_edges(
        "previsor", tools_condition,
        {"tools": "ferramentas_previsao", END: "registrar_previsao"},
    )
    g.add_edge("ferramentas_previsao", "previsor")
    g.add_edge("registrar_previsao", END)
    return g.compile()


def _subgrafo_critica():
    g = StateGraph(EstadoColeta)
    g.add_node("critico", no_critico)
    g.add_node("registrar_parecer", no_registrar_parecer)
    g.add_edge(START, "critico")
    g.add_edge("critico", "registrar_parecer")
    g.add_edge("registrar_parecer", END)
    return g.compile()


def _subgrafo_redacao():
    g = StateGraph(EstadoColeta)
    g.add_node("redator", no_redator)
    g.add_node("registrar_relatorio", no_registrar_relatorio)
    g.add_edge(START, "redator")
    g.add_edge("redator", "registrar_relatorio")
    g.add_edge("registrar_relatorio", END)
    return g.compile()


# --- A decisão do supervisor --------------------------------------------------
def decidir_apos_critica(estado: EstadoColeta) -> str:
    """Depois da crítica: volta para o previsor ou segue para a redação?

    Três saídas possíveis:
      - "redacao": o crítico aprovou. Caminho feliz.
      - "previsao": reprovou e ainda há tentativa disponível — o previsor recebe
        o parecer e tenta de novo.
      - "redacao" (por esgotamento): reprovou, mas o teto estourou. O relatório
        sai assim mesmo, com a ressalva. Não insistimos para sempre.
    """
    parecer = estado.get("parecer") or {}
    tentativas = estado.get("tentativas", 0)

    if parecer.get("decisao") == "aprova":
        return "redacao"

    if tentativas >= TETO_TENTATIVAS:
        return "redacao"

    return "previsao"


# --- Montagem do grafo do time ------------------------------------------------
def montar_time(checkpointer=None):
    """Monta o grafo que amarra os quatro agentes."""
    grafo = StateGraph(EstadoColeta)

    grafo.add_node("coleta", _subgrafo_coleta())
    grafo.add_node("previsao", _subgrafo_previsao())
    grafo.add_node("critica", _subgrafo_critica())
    grafo.add_node("redacao", _subgrafo_redacao())

    grafo.add_edge(START, "coleta")
    grafo.add_edge("coleta", "previsao")
    grafo.add_edge("previsao", "critica")
    grafo.add_conditional_edges(
        "critica",
        decidir_apos_critica,
        {"previsao": "previsao", "redacao": "redacao"},
    )
    grafo.add_edge("redacao", END)

    return grafo.compile(checkpointer=checkpointer)


def rodar_time(pedido: str, indicador: str = "ipca", verboso: bool = True,
               publicar_resultado: bool = True) -> dict:
    """Roda o time inteiro a partir de um pedido em linguagem natural.

    Args:
        pedido: o que se quer, em português. Ex.: "Preveja o IPCA do próximo
            mês." O coletor interpreta o pedido e decide o que buscar.
        indicador: chave do indicador — vira thread_id do checkpoint e guia a
            validação de faixa.
        verboso: imprime o progresso de cada etapa enquanto roda.
        publicar_resultado: ao fim de uma rodada completa, grava
            data/resultado.json — o arquivo que o dashboard lê. Rodada
            incompleta NÃO sobrescreve o arquivo bom da rodada anterior.

    Returns:
        O estado final, com série, previsão, parecer e relatório. Quando
        `publicar_resultado`, ganha também a chave "publicacao" com o que
        aconteceu na gravação.
    """
    from langchain_core.messages import HumanMessage

    estado_inicial = {
        "messages": [HumanMessage(content=pedido)],
        "indicador": indicador,
        "validados": [],
        "avisos": [],
        "tentativas": 0,
    }

    with SqliteSaver.from_conn_string(_DB_CHECKPOINTS) as memoria:
        time = montar_time(checkpointer=memoria)
        config = {"configurable": {"thread_id": f"time-{indicador}"},
                  # O laço previsor<->crítico pode dar várias voltas; o padrão do
                  # LangGraph (25) é folgado, mas deixamos explícito.
                  "recursion_limit": 50}

        if not verboso:
            estado_final = time.invoke(estado_inicial, config=config)
            return _publicar(estado_final, indicador, publicar_resultado, verboso)

        # stream por nó: mostra o time trabalhando, etapa a etapa.
        for evento in time.stream(estado_inicial, config=config, stream_mode="updates"):
            for etapa, atualizacao in evento.items():
                _mostrar_etapa(etapa, atualizacao)

        # O stream devolve só os deltas; para o estado completo, lemos o
        # checkpoint no fim.
        estado_final = time.get_state(config).values
        return _publicar(estado_final, indicador, publicar_resultado, verboso)


def _publicar(estado: dict, indicador: str, ativado: bool, verboso: bool) -> dict:
    """Grava o resultado para o dashboard e anota no estado o que aconteceu.

    Nunca derruba a rodada: uma falha ao publicar é informação, não motivo de
    parada. O time já fez o trabalho; se o resultado não era publicável, o
    arquivo da rodada anterior continua onde estava.
    """
    if not ativado:
        return estado

    from publicacao import publicar_se_completa

    saida = publicar_se_completa(estado, indicador=indicador)
    estado["publicacao"] = saida

    if verboso:
        if saida["publicado"]:
            print(f"[PUBLICADO] data/resultado.json — o dashboard já pode ler.")
        else:
            print(f"[NÃO PUBLICADO] {saida['motivo']}")
            if saida["arquivo_anterior_preservado"]:
                print("              O resultado da rodada anterior foi "
                      "preservado.")

    return estado


def _mostrar_etapa(etapa: str, atualizacao: dict | None) -> None:
    """Imprime o que cada etapa produziu, em uma linha."""
    rotulos = {
        "coleta": "COLETA",
        "previsao": "PREVISÃO",
        "critica": "CRÍTICA",
        "redacao": "REDAÇÃO",
    }
    rotulo = rotulos.get(etapa, etapa.upper())
    atualizacao = atualizacao or {}

    if etapa == "coleta":
        pontos = atualizacao.get("validados") or []
        print(f"[{rotulo}] {len(pontos)} ponto(s) validado(s).")
    elif etapa == "previsao":
        prev = atualizacao.get("previsao") or {}
        tentativa = atualizacao.get("tentativas", "?")
        if prev.get("status") == "ok":
            iv = prev.get("intervalo") or {}
            print(f"[{rotulo}] tentativa {tentativa}: {prev['valor']}% "
                  f"[{iv.get('minimo')}, {iv.get('maximo')}] via {prev.get('modelo')}")
        else:
            print(f"[{rotulo}] tentativa {tentativa}: sem previsão "
                  f"({prev.get('status', '?')})")
    elif etapa == "critica":
        par = atualizacao.get("parecer") or {}
        decisao = par.get("decisao", "?")
        motivos = par.get("motivos") or []
        print(f"[{rotulo}] {decisao.upper()}"
              + (f" — {motivos[0][:80]}" if motivos else ""))
    elif etapa == "redacao":
        print(f"[{rotulo}] relatório escrito.")


if __name__ == "__main__":
    import sys

    pedido = " ".join(sys.argv[1:]) or (
        "Colete o IPCA (variação mensal) desde janeiro de 2023 e preveja o "
        "próximo mês."
    )

    print(f"PEDIDO: {pedido}\n")
    estado = rodar_time(pedido)

    print("\n" + "=" * 70)
    print("RELATÓRIO FINAL")
    print("=" * 70)
    print(estado.get("relatorio") or "(nenhum relatório foi produzido)")

    avisos = estado.get("avisos") or []
    if avisos:
        print("\nAVISOS:")
        for a in avisos:
            print(f"  - {a}")

    print(f"\nTentativas de previsão: {estado.get('tentativas', 0)}")
    parecer = estado.get("parecer") or {}
    if parecer.get("decisao") == "rejeita":
        print(f"Situação: previsão NÃO aprovada pelo crítico.")
