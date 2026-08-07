# previsor.py
#
# Agente Previsor (LangGraph + Gemini Flash Lite).
#
# Papel: pegar a série que o coletor deixou no mural (estado["validados"]) e
# prever o próximo valor do IPCA. O Gemini decide RODAR o modelo e como reagir ao
# resultado; quem calcula o número é o ARIMA do statsmodels, em tools/arima.py.
#
# Se o crítico reprovou a rodada anterior, o parecer entra no pedido e o agente
# deve mudar algo no modelo em vez de repetir a mesma conta.
#
# Grafo:  previsor ──(precisa de tool?)──> ferramentas_previsao ──> previsor
#                └──(não)──> registrar_previsao ──> FIM

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from state import EstadoColeta
from tools.arima import prever_arima

load_dotenv()

_PROMPT = (Path(__file__).parent / "prompts" / "previsor.md").read_text(encoding="utf-8")

# temperature=0: previsão é tarefa determinística; não queremos criatividade.
_llm = ChatGoogleGenerativeAI(model="gemini-flash-lite-latest", temperature=0)
_TOOLS_PREVISAO = [prever_arima]
_llm_com_tools = _llm.bind_tools(_TOOLS_PREVISAO)


# --- Nó: previsor -------------------------------------------------------------
def no_previsor(estado: EstadoColeta) -> dict:
    """Chama o Gemini com o system prompt, o pedido e a conversa DESTA previsão.

    Duas coisas precisam ser verdade ao mesmo tempo:

    1. O previsor não herda a conversa do coletor (que é longa e fala de outra
       coisa) — por isso montamos um pedido limpo com a série.
    2. Mas ele PRECISA ver o resultado das tools que ele mesmo chamou nesta
       rodada. Sem isso o modelo não percebe que já calculou, chama prever_arima
       de novo, e o laço previsor <-> ferramentas gira até estourar a recursão.

    A solução é `_conversa_da_previsao`: pega só o trecho a partir da primeira
    chamada de tool do previsor, descartando o histórico da coleta.
    """
    pedido = _montar_pedido(estado)
    mensagens = [
        SystemMessage(content=_PROMPT),
        HumanMessage(content=pedido),
        *_conversa_da_previsao(estado),
    ]
    resposta = _llm_com_tools.invoke(mensagens)
    return {"messages": [resposta]}


def _conversa_da_previsao(estado: EstadoColeta) -> list:
    """Devolve só as mensagens desta previsão (chamadas de tool e resultados).

    Varre o mural de trás para frente e para na primeira mensagem que não
    pertence ao ciclo do previsor. O que interessa é o par
    AIMessage(tool_calls) + ToolMessage: é ele que informa ao modelo que a conta
    já foi feita e qual foi o resultado.
    """
    mensagens = estado.get("messages", []) or []
    trecho: list = []
    for msg in reversed(mensagens):
        if isinstance(msg, ToolMessage):
            trecho.append(msg)
            continue
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            trecho.append(msg)
            continue
        # Mensagem que não é do ciclo de tools (ex.: a conversa da coleta):
        # daqui para trás não interessa.
        break
    trecho.reverse()

    # Uma ToolMessage órfã (sem a AIMessage que a pediu) faz a API do Gemini
    # recusar o pedido. Se o trecho começar com ToolMessage, descartamos.
    while trecho and isinstance(trecho[0], ToolMessage):
        trecho.pop(0)

    return trecho


def _montar_pedido(estado: EstadoColeta) -> str:
    """Monta o texto do pedido: a série + (se for retentativa) o parecer.

    É aqui que o previsor "leva em conta o motivo da reprovação": o parecer do
    crítico entra no pedido, com instrução explícita de mudar o modelo.
    """
    serie = estado.get("validados", []) or []
    indicador = estado.get("indicador", "ipca")

    partes = [
        f"Série de '{indicador}' já coletada e validada "
        f"({len(serie)} observação(ões)), em JSON:",
        json.dumps(serie, ensure_ascii=False),
        "",
        "Preveja o próximo valor chamando a ferramenta prever_arima com esses pontos.",
    ]

    parecer = estado.get("parecer") or {}
    tentativas = estado.get("tentativas", 0)
    if parecer:
        # RETENTATIVA: o crítico reprovou. O motivo vai explícito no pedido.
        partes += [
            "",
            f"ATENÇÃO — esta é a tentativa nº {tentativas + 1}. O Agente Crítico "
            f"REPROVOU a previsão anterior com este parecer:",
            json.dumps(parecer, ensure_ascii=False),
            "",
            "Leia o motivo e MUDE a forma de calcular (normalmente a ordem do "
            "modelo: ordem_p, ordem_d, ordem_q). Rodar de novo com os mesmos "
            "parâmetros devolveria exatamente o mesmo número e desperdiçaria a "
            "rodada.",
        ]
        previsao_anterior = estado.get("previsao") or {}
        if previsao_anterior:
            partes += [
                "",
                "Previsão anterior (a que foi reprovada):",
                json.dumps(previsao_anterior, ensure_ascii=False),
            ]

    return "\n".join(partes)


# --- Nó: ferramentas ----------------------------------------------------------
no_ferramentas_previsao = ToolNode(_TOOLS_PREVISAO)


# --- Nó: registrar_previsao ---------------------------------------------------
def no_registrar_previsao(estado: EstadoColeta) -> dict:
    """Extrai o resultado do ARIMA das ToolMessages e escreve no mural.

    Escreve `previsao` (o que o crítico vai ler) e incrementa `tentativas`. Como
    cada nó devolve só o campo que é dele, o incremento é explícito: lê o valor
    atual do mural e devolve valor + 1.

    Se a ferramenta recusou por falta de histórico, isso NÃO é maquiado: a
    recusa vai para `previsao` com o status, e o motivo entra em `avisos`.
    """
    resultado = _ultimo_resultado_arima(estado)
    tentativas = estado.get("tentativas", 0) + 1

    # Nenhuma tool rodou: o agente respondeu sem calcular. Não inventamos número.
    if resultado is None:
        return {
            "previsao": {
                "status": "sem_previsao",
                "motivo": "o agente não chamou a ferramenta de previsão — "
                          "nenhum número foi calculado.",
            },
            "tentativas": tentativas,
            "avisos": ["previsor: nenhuma chamada a prever_arima nesta rodada."],
        }

    status = resultado.get("status")

    if status == "ok":
        previsao = {
            "status": "ok",
            "valor": resultado["valor"],
            "intervalo": resultado["intervalo"],
            "modelo": resultado["modelo"],
            "observacoes": resultado["observacoes"],
            "ultima_data": resultado.get("ultima_data"),
        }
        return {"previsao": previsao, "tentativas": tentativas}

    # historico_insuficiente ou falha_no_modelo: a recusa É o resultado.
    motivo = resultado.get("motivo", "(sem motivo informado)")
    return {
        "previsao": resultado,
        "tentativas": tentativas,
        "avisos": [f"previsor: sem previsão ({status}) — {motivo}"],
    }


def _ultimo_resultado_arima(estado: EstadoColeta) -> dict | None:
    """Pega o resultado da ÚLTIMA chamada a prever_arima na conversa.

    Numa retentativa o agente pode ter rodado o modelo mais de uma vez; a rodada
    mais recente é a que vale, porque é a que respondeu ao parecer do crítico.
    """
    for msg in reversed(estado.get("messages", []) or []):
        if not isinstance(msg, ToolMessage):
            continue
        conteudo = msg.content
        if isinstance(conteudo, dict):
            return conteudo
        if isinstance(conteudo, str):
            try:
                dado = json.loads(conteudo)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(dado, dict) and "status" in dado:
                return dado
    return None


# --- Montagem do grafo --------------------------------------------------------
def montar_grafo_previsor(checkpointer=None):
    """Monta e compila o grafo do previsor.

    Fica como função (e não só como constante) para o supervisor poder montá-lo
    com o mesmo checkpointer do resto do time, quando essa hora chegar.
    """
    grafo = StateGraph(EstadoColeta)
    grafo.add_node("previsor", no_previsor)
    grafo.add_node("ferramentas_previsao", no_ferramentas_previsao)
    grafo.add_node("registrar_previsao", no_registrar_previsao)

    grafo.add_edge(START, "previsor")
    grafo.add_conditional_edges(
        "previsor",
        tools_condition,
        {"tools": "ferramentas_previsao", END: "registrar_previsao"},
    )
    grafo.add_edge("ferramentas_previsao", "previsor")
    grafo.add_edge("registrar_previsao", END)

    return grafo.compile(checkpointer=checkpointer)


grafo_previsor = montar_grafo_previsor()


def prever(estado: EstadoColeta) -> dict:
    """Roda o previsor sobre um estado que já tem a série coletada.

    Devolve o estado final (com `previsao` e `tentativas` preenchidos).
    """
    return grafo_previsor.invoke(estado)


if __name__ == "__main__":
    from memoria import carregar_pontos

    # Lê a série que o coletor já persistiu (memória nível 1).
    pontos = [
        {"data": data, "valor": valor}
        for data, valor in sorted(carregar_pontos("ipca").items())
    ]

    estado_final = prever({
        "messages": [],
        "indicador": "ipca",
        "validados": pontos,
        "avisos": [],
    })

    print("\n=== RESPOSTA DO PREVISOR ===")
    print(estado_final["messages"][-1].content)
    print("\n=== PREVISÃO (no mural) ===")
    print(json.dumps(estado_final.get("previsao"), ensure_ascii=False, indent=2))
    print(f"\n=== TENTATIVAS === {estado_final.get('tentativas')}")
    print("\n=== AVISOS ===")
    for a in estado_final.get("avisos") or ["(nenhum)"]:
        print(f"  - {a}")
