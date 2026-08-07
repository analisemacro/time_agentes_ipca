# coletor.py
#
# Agente de Coleta (LangGraph + Gemini Flash Lite).
#
# Único agente do projeto por enquanto — por isso mora na raiz, não em agents/.
# Papel: orquestrar a coleta das séries macroeconômicas. O Gemini decide qual
# ferramenta chamar; quem fala com as APIs e entrega o dado REAL são as tools em
# tools/. O nó `guardar` roda a validação em camadas antes de dar por encerrado.
#
# Grafo:  agente ──(precisa de tool?)──> ferramentas ──> agente
#              └──(não)──> guardar ──> FIM

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from memoria import salvar_pontos
from state import EstadoColeta
from tools import TOOLS
from tools.schema import valida_pontos

# Caminho do banco de checkpoints do grafo (memória nível 2). Fica em data/.
_DB_CHECKPOINTS = str(Path(__file__).parent / "data" / "checkpoints.sqlite")

# Carrega GOOGLE_API_KEY do .env antes de instanciar o modelo.
load_dotenv()

# --- LLM + tools --------------------------------------------------------------
# temperature=0: coleta é tarefa determinística; não queremos criatividade.
_PROMPT = (Path(__file__).parent / "prompts" / "coletor.md").read_text(encoding="utf-8")

_llm = ChatGoogleGenerativeAI(model="gemini-flash-lite-latest", temperature=0)
# bind_tools é o que deixa o modelo ESCOLHER a ferramenta a chamar.
_llm_com_tools = _llm.bind_tools(TOOLS)


# --- Nó: agente ---------------------------------------------------------------
def no_agente(estado: EstadoColeta) -> dict:
    """Chama o Gemini com o system prompt + a conversa até aqui.

    Devolve só a nova mensagem da IA; add_messages a ACRESCENTA ao mural.
    """
    mensagens = [SystemMessage(content=_PROMPT), *estado["messages"]]
    resposta = _llm_com_tools.invoke(mensagens)
    return {"messages": [resposta]}


# --- Nó: ferramentas ----------------------------------------------------------
# ToolNode executa as tool calls que o agente pediu e devolve ToolMessages.
no_ferramentas = ToolNode(TOOLS)


# --- Nó: guardar --------------------------------------------------------------
def no_guardar(estado: EstadoColeta) -> dict:
    """Roda a validação em camadas sobre os pontos que as tools coletaram.

    Varre as ToolMessages da conversa, junta os pontos, valida contra o indicador
    do estado e escreve (validados, avisos) no mural. Preferimos registrar o
    aviso a descartar em silêncio — nada de número errado passando batido.
    """
    indicador = estado.get("indicador", "")
    pontos: list[dict] = []
    for msg in estado["messages"]:
        if isinstance(msg, ToolMessage):
            payload = _extrair_pontos(msg.content)
            pontos.extend(payload)

    # Deduplica por data ANTES de validar.
    #
    # Por que é preciso: o checkpoint guarda o histórico da thread, então numa
    # segunda execução do mesmo indicador as ToolMessages das rodadas anteriores
    # ainda estão no mural. Sem isto, a série sairia daqui com cada ponto
    # repetido uma vez por execução — e o previsor receberia essa série inflada.
    #
    # O último vence: se a mesma data aparecer de novo, ficamos com a coleta mais
    # recente (é a revisão mais provável do dado).
    unicos: dict[str, dict] = {}
    for p in pontos:
        chave = str(p.get("data", ""))
        if chave:
            unicos[chave] = p
    pontos = [unicos[k] for k in sorted(unicos)]

    validados, avisos = valida_pontos(pontos, indicador)

    # Memória nível 1: junta os pontos validados ao JSON do indicador (merge por
    # data, sem duplicar). Só persistimos o que passou na validação.
    if validados:
        resumo = salvar_pontos(indicador, validados)
        avisos = avisos + [
            f"persistido em data/{indicador}.json: {resumo['novos']} novo(s), "
            f"{resumo['atualizados']} atualizado(s), {resumo['total']} no total."
        ]

    # Guardamos no estado como dicts serializáveis (não objetos Pydantic): o
    # estado inteiro vai para o checkpoint SQLite, que exige tipos simples.
    validados_serializaveis = [
        {"data": p.data.isoformat(), "valor": p.valor} for p in validados
    ]
    return {"validados": validados_serializaveis, "avisos": avisos}


def _extrair_pontos(conteudo) -> list[dict]:
    """Extrai a lista de pontos {"data","valor"} do retorno de uma tool.

    O ToolNode serializa o dict de retorno da tool como texto JSON; parseamos e
    pegamos a chave "pontos". Se não der para parsear, ignoramos (a validação
    cuida do resto) — nunca inventamos pontos.
    """
    if isinstance(conteudo, dict):
        return conteudo.get("pontos", []) or []
    if isinstance(conteudo, str):
        try:
            dado = json.loads(conteudo)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(dado, dict):
            return dado.get("pontos", []) or []
    return []


# --- Montagem do grafo --------------------------------------------------------
def _montar(checkpointer=None):
    """Monta e compila o grafo. checkpointer = memória nível 2 (estado do grafo)."""
    grafo = StateGraph(EstadoColeta)
    grafo.add_node("agente", no_agente)
    grafo.add_node("ferramentas", no_ferramentas)
    grafo.add_node("guardar", no_guardar)

    grafo.add_edge(START, "agente")

    # Aresta condicional: se a última mensagem do agente tiver tool calls,
    # tools_condition roteia para "ferramentas"; senão, para "guardar".
    grafo.add_conditional_edges(
        "agente",
        tools_condition,
        {"tools": "ferramentas", END: "guardar"},
    )
    grafo.add_edge("ferramentas", "agente")  # volta pro agente ler o resultado
    grafo.add_edge("guardar", END)

    return grafo.compile(checkpointer=checkpointer)


# Grafo SEM checkpointer, pronto para importar/inspecionar (ex.: desenhar o
# diagrama). Para rodar com memória de estado, use coletar() abaixo.
grafo_coleta = _montar()


def coletar(pedido: str, indicador: str) -> dict:
    """Executa o Agente de Coleta com os DOIS níveis de memória.

    - Nível 2 (estado do grafo): SqliteSaver com thread_id=indicador, de modo que
      cada indicador tem sua própria linha do tempo de execuções — retomar a
      coleta do "ipca" não se mistura com a do "cambio".
    - Nível 1 (dados): feito dentro do nó `guardar`, via memoria.salvar_pontos.

    Args:
        pedido: instrução em linguagem natural para o agente.
        indicador: chave do indicador (ex.: "ipca") — vira thread_id e guia a
            validação de faixa e o arquivo data/<indicador>.json.
    """
    from langchain_core.messages import HumanMessage

    # from_conn_string é um context manager — o checkpointer vive dentro do with.
    with SqliteSaver.from_conn_string(_DB_CHECKPOINTS) as memoria_estado:
        grafo = _montar(checkpointer=memoria_estado)
        config = {"configurable": {"thread_id": indicador}}
        return grafo.invoke(
            {
                "messages": [HumanMessage(content=pedido)],
                "indicador": indicador,
                "validados": [],
                "avisos": [],
            },
            config=config,
        )


if __name__ == "__main__":
    estado_final = coletar(
        pedido="Colete o IPCA (variação mensal) a partir de janeiro de 2026.",
        indicador="ipca",
    )

    print("\n=== RESPOSTA DO AGENTE ===")
    print(estado_final["messages"][-1].content)
    print("\n=== VALIDADOS ===")
    for p in estado_final["validados"]:
        print(f"  {p['data']}  {p['valor']}")
    print("\n=== AVISOS ===")
    for a in estado_final["avisos"] or ["(nenhum)"]:
        print(f"  - {a}")
