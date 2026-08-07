# critico.py
#
# Agente Crítico (LangGraph + Gemini Flash Lite).
#
# Papel: tentar DERRUBAR a previsão que o Previsor deixou no mural. Não coleta
# dado, não roda modelo, não calcula previsão — lê a série (estado["validados"]),
# a previsão (estado["previsao"]) e um diagnóstico com as medidas já calculadas
# em código puro (tools/diagnostico.py), e devolve um veredito.
#
# Por que o diagnóstico é código e não LLM: as três suspeitas (salto vs.
# tendência, largura do intervalo, ajuste forçado) são comparações numéricas.
# Deixar o modelo de linguagem fazer essa aritmética de cabeça seria o mesmo
# chute que o projeto evita em todo lugar. O código MEDE; o agente JULGA.
#
# Grafo:  critico ──> registrar_parecer ──> FIM
# (sem ToolNode: o crítico não chama ferramenta, só lê e opina)

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph

from state import EstadoColeta
from texto import texto_da_resposta
from tools.diagnostico import diagnosticar

load_dotenv()

_PROMPT = (Path(__file__).parent / "prompts" / "critico.md").read_text(encoding="utf-8")

# temperature=0: queremos um crítico consistente, não criativo.
_llm = ChatGoogleGenerativeAI(model="gemini-flash-lite-latest", temperature=0)

DECISOES_VALIDAS = {"aprova", "rejeita"}


# --- Nó: critico --------------------------------------------------------------
def no_critico(estado: EstadoColeta) -> dict:
    """Chama o Gemini com a série, a previsão e o diagnóstico já calculado."""
    pedido = _montar_pedido(estado)
    mensagens = [SystemMessage(content=_PROMPT), HumanMessage(content=pedido)]
    resposta = _llm.invoke(mensagens)
    return {"messages": [resposta]}


def _montar_pedido(estado: EstadoColeta) -> str:
    """Monta o pedido: previsão + diagnóstico + série (nessa ordem de destaque).

    A série vai resumida (últimos 12 pontos): o crítico julga a previsão contra a
    tendência recente, e a série inteira só encheria o contexto.
    """
    serie = estado.get("validados", []) or []
    previsao = estado.get("previsao") or {}
    indicador = estado.get("indicador", "ipca")

    diagnostico = diagnosticar(serie, previsao)
    recentes = serie[-12:]

    partes = [
        f"Avalie a previsão de '{indicador}' abaixo. Tente derrubá-la antes de aceitá-la.",
        "",
        "PREVISÃO DO PREVISOR:",
        json.dumps(previsao, ensure_ascii=False, indent=2),
        "",
        "DIAGNÓSTICO (medidas já calculadas — use estes números):",
        json.dumps(diagnostico, ensure_ascii=False, indent=2),
        "",
        f"SÉRIE RECENTE (últimos {len(recentes)} de {len(serie)} pontos):",
        json.dumps(recentes, ensure_ascii=False),
        "",
        "Responda apenas com o JSON do parecer, no formato especificado.",
    ]
    return "\n".join(partes)


# --- Nó: registrar_parecer ----------------------------------------------------
def no_registrar_parecer(estado: EstadoColeta) -> dict:
    """Lê a resposta do crítico, valida o formato e escreve `parecer` no mural.

    Rede de segurança: se o LLM devolver algo que não é o JSON esperado, NÃO
    inventamos um veredito. Rejeitamos por precaução: uma previsão aprovada por
    falha de formato seguiria para o relatório sem ninguém ter avaliado o número.
    """
    previsao = estado.get("previsao") or {}

    # Sem previsão numérica não há o que criticar. O parecer registra isso em vez
    # de fingir uma avaliação.
    if previsao.get("status") != "ok":
        return {
            "parecer": {
                "decisao": "rejeita",
                "motivos": [
                    f"não há previsão para avaliar (status: "
                    f"{previsao.get('status', 'ausente')})."
                ],
                "o_que_corrigir": previsao.get(
                    "motivo", "produza uma previsão antes de submetê-la ao crítico."
                ),
                "confianca": "alta",
                "origem": "regra",  # veio do código, não do LLM
            }
        }

    bruto = _ultima_resposta(estado)
    parecer = _parsear_parecer(bruto)

    if parecer is None:
        return {
            "parecer": {
                "decisao": "rejeita",
                "motivos": [
                    "o parecer do crítico não veio no formato esperado — "
                    "rejeitado por precaução, não por mérito da previsão."
                ],
                "o_que_corrigir": "reenvie a previsão para nova avaliação.",
                "confianca": "baixa",
                "origem": "falha_de_formato",
            },
            "avisos": ["crítico: resposta fora do formato JSON esperado."],
        }

    return {"parecer": parecer}


def _ultima_resposta(estado: EstadoColeta) -> str:
    """Pega o texto da última mensagem da IA na conversa."""
    for msg in reversed(estado.get("messages", []) or []):
        if isinstance(msg, AIMessage):
            return texto_da_resposta(msg.content)
    return ""


def _parsear_parecer(texto: str) -> dict | None:
    """Extrai o JSON do parecer do texto do LLM. None se não der para confiar.

    Tolera o modelo embrulhar o JSON numa cerca ```json ... ``` ou soltar um
    parágrafo antes — mas não inventa campo que não veio.
    """
    if not texto or not texto.strip():
        return None

    candidato = texto.strip()

    # Tira a cerca de código, se houver.
    if "```" in candidato:
        partes = candidato.split("```")
        for parte in partes:
            limpa = parte.strip()
            if limpa.startswith("json"):
                limpa = limpa[4:].strip()
            if limpa.startswith("{"):
                candidato = limpa
                break

    # Recorta do primeiro { ao último } — resolve prosa em volta do JSON.
    inicio, fim = candidato.find("{"), candidato.rfind("}")
    if inicio == -1 or fim == -1 or fim <= inicio:
        return None
    candidato = candidato[inicio:fim + 1]

    try:
        dado = json.loads(candidato)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(dado, dict):
        return None

    decisao = str(dado.get("decisao", "")).strip().lower()
    if decisao not in DECISOES_VALIDAS:
        return None

    # Motivos: exigimos pelo menos um quando rejeita. Rejeição sem motivo não
    # serve para o previsor corrigir nada.
    motivos = dado.get("motivos") or []
    if isinstance(motivos, str):
        motivos = [motivos]
    motivos = [str(m).strip() for m in motivos if str(m).strip()]
    if decisao == "rejeita" and not motivos:
        return None

    return {
        "decisao": decisao,
        "motivos": motivos,
        "o_que_corrigir": str(dado.get("o_que_corrigir", "")).strip(),
        "confianca": str(dado.get("confianca", "media")).strip().lower(),
        "origem": "llm",
    }


# --- Montagem do grafo --------------------------------------------------------
def montar_grafo_critico(checkpointer=None):
    """Monta e compila o grafo do crítico."""
    grafo = StateGraph(EstadoColeta)
    grafo.add_node("critico", no_critico)
    grafo.add_node("registrar_parecer", no_registrar_parecer)

    grafo.add_edge(START, "critico")
    grafo.add_edge("critico", "registrar_parecer")
    grafo.add_edge("registrar_parecer", END)

    return grafo.compile(checkpointer=checkpointer)


grafo_critico = montar_grafo_critico()


def criticar(estado: EstadoColeta) -> dict:
    """Roda o crítico sobre um estado que já tem série e previsão."""
    return grafo_critico.invoke(estado)


if __name__ == "__main__":
    from memoria import carregar_pontos

    pontos = [
        {"data": data, "valor": valor}
        for data, valor in sorted(carregar_pontos("ipca").items())
    ]

    # Previsão de exemplo para exercitar o crítico sem depender do previsor.
    previsao_exemplo = {
        "status": "ok",
        "valor": 1.60,
        "intervalo": {"minimo": 1.40, "maximo": 1.80, "nivel": 0.95},
        "modelo": "ARIMA(1,0,1)",
        "observacoes": len(pontos),
        "ultima_data": pontos[-1]["data"] if pontos else None,
    }

    estado_final = criticar({
        "messages": [],
        "indicador": "ipca",
        "validados": pontos,
        "avisos": [],
        "previsao": previsao_exemplo,
        "tentativas": 1,
    })

    print("\n=== PARECER (no mural) ===")
    print(json.dumps(estado_final.get("parecer"), ensure_ascii=False, indent=2))
