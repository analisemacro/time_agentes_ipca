# redator.py
#
# Agente Redator (LangGraph + Gemini Flash Lite).
#
# Papel: transformar a previsão (estado["previsao"]) e o parecer do crítico
# (estado["parecer"]) num relatório curto em português. Não coleta, não calcula,
# não julga — só escreve o que os outros produziram.
#
# A regra que não pode falhar: se o crítico REPROVOU, isso aparece no texto. Não
# confiamos essa regra só ao prompt — depois que o LLM escreve, o nó
# `registrar_relatorio` CONFERE se a ressalva está lá e, se não estiver, prefixa
# um aviso ao relatório. Prompt é instrução; código é garantia.
#
# Grafo:  redator ──> registrar_relatorio ──> FIM

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph

from state import EstadoColeta
from texto import texto_da_resposta

load_dotenv()

_PROMPT = (Path(__file__).parent / "prompts" / "redator.md").read_text(encoding="utf-8")

# temperature=0.3: um pouco de liberdade para o texto não sair robótico, mas
# longe do suficiente para inventar. Os números vêm prontos no pedido.
_llm = ChatGoogleGenerativeAI(model="gemini-flash-lite-latest", temperature=0.3)

# Aviso que o código prefixa quando o LLM omite a reprovação. Fica em constante
# para o supervisor poder detectar que a rede de segurança entrou em ação.
AVISO_REPROVACAO = "ATENÇÃO — PREVISÃO NÃO APROVADA NA REVISÃO CRÍTICA."


# --- Nó: redator --------------------------------------------------------------
def no_redator(estado: EstadoColeta) -> dict:
    """Chama o Gemini com a previsão e o parecer já formatados."""
    pedido = _montar_pedido(estado)
    mensagens = [SystemMessage(content=_PROMPT), HumanMessage(content=pedido)]
    resposta = _llm.invoke(mensagens)
    return {"messages": [resposta]}


def _montar_pedido(estado: EstadoColeta) -> str:
    """Monta o pedido do relatório: previsão + parecer + contexto da série."""
    previsao = estado.get("previsao") or {}
    parecer = estado.get("parecer") or {}
    indicador = estado.get("indicador", "ipca")
    serie = estado.get("validados", []) or []
    tentativas = estado.get("tentativas", 0)

    partes = [
        f"Escreva o relatório da previsão de '{indicador}'.",
        "",
        "PREVISÃO:",
        json.dumps(previsao, ensure_ascii=False, indent=2),
        "",
        "PARECER DO CRÍTICO:",
        json.dumps(parecer, ensure_ascii=False, indent=2),
        "",
        f"Contexto: a série tem {len(serie)} observação(ões) e o previsor fez "
        f"{tentativas} tentativa(s) até aqui.",
    ]

    # Reforço explícito no pedido quando houve reprovação. O prompt já manda, mas
    # repetir aqui reduz a chance de o modelo "esquecer" no meio do texto.
    if parecer.get("decisao") == "rejeita":
        partes += [
            "",
            "IMPORTANTE: o crítico REPROVOU esta previsão. O relatório TEM de "
            "deixar isso claro logo no começo, com o motivo. Não suavize e não "
            "deixe a ressalva para o final.",
        ]

    return "\n".join(partes)


# --- Nó: registrar_relatorio --------------------------------------------------
def no_registrar_relatorio(estado: EstadoColeta) -> dict:
    """Escreve `relatorio` no mural, garantindo que a ressalva não sumiu.

    Rede de segurança: se o crítico reprovou e o texto do LLM não menciona isso,
    o código prefixa o aviso. Um relatório que omite a reprovação faz o leitor
    tratar como validado um número que o crítico recusou.
    """
    parecer = estado.get("parecer") or {}
    previsao = estado.get("previsao") or {}
    texto = _ultima_resposta(estado).strip()

    # O LLM não escreveu nada: montamos um relatório mínimo em código puro, sem
    # inventar número.
    if not texto:
        return {
            "relatorio": _relatorio_de_emergencia(previsao, parecer),
            "avisos": ["redator: o modelo não produziu texto; relatório gerado "
                       "pelo código a partir dos dados do mural."],
        }

    reprovado = parecer.get("decisao") == "rejeita"
    if reprovado and not _menciona_reprovacao(texto):
        # O modelo omitiu a ressalva. Não reescrevemos o texto dele — prefixamos
        # o aviso e o motivo, para o leitor ver a reprovação antes do resto.
        motivos = parecer.get("motivos") or []
        motivo = motivos[0] if motivos else "sem motivo registrado"
        texto = f"{AVISO_REPROVACAO} Motivo: {motivo}\n\n{texto}"
        return {
            "relatorio": texto,
            "avisos": ["redator: o texto omitia a reprovação do crítico — "
                       "aviso inserido pelo código."],
        }

    return {"relatorio": texto}


def _menciona_reprovacao(texto: str) -> bool:
    """Checa se o texto sinaliza que a previsão não foi aprovada.

    Busca por marcas de negação/reprovação, não por uma frase exata: o modelo
    escreve de formas diferentes a cada rodada. Falso negativo aqui só faz o
    código prefixar um aviso a mais — erra para o lado seguro.
    """
    minusculo = texto.lower()
    marcas = [
        "não foi aprovada", "nao foi aprovada",
        "não aprovada", "nao aprovada",
        "não passou", "nao passou",
        "reprovada", "reprovado", "rejeitada", "rejeitado",
        "não foi validada", "nao foi validada",
        "ressalva", "não aprovou", "nao aprovou",
    ]
    return any(m in minusculo for m in marcas)


def _relatorio_de_emergencia(previsao: dict, parecer: dict) -> str:
    """Monta um relatório mínimo em código puro, sem LLM e sem inventar nada."""
    if previsao.get("status") != "ok":
        motivo = previsao.get("motivo", "motivo não registrado")
        return (f"Não houve previsão do IPCA neste ciclo "
                f"(status: {previsao.get('status', 'ausente')}). {motivo}")

    valor = previsao.get("valor")
    intervalo = previsao.get("intervalo") or {}
    partes = [
        f"O modelo projeta {valor}% para o IPCA, com intervalo de "
        f"{intervalo.get('minimo')}% a {intervalo.get('maximo')}% "
        f"({int(float(intervalo.get('nivel', 0.95)) * 100)}% de confiança)."
    ]
    if parecer.get("decisao") == "rejeita":
        motivos = parecer.get("motivos") or ["sem motivo registrado"]
        partes.append(f"{AVISO_REPROVACAO} Motivo: {motivos[0]}")
    partes.append(
        f"O número saiu de um {previsao.get('modelo', 'modelo não identificado')} "
        f"estimado sobre {previsao.get('observacoes', '?')} observações."
    )
    return " ".join(partes)


def _ultima_resposta(estado: EstadoColeta) -> str:
    """Pega o texto da última mensagem da IA na conversa."""
    for msg in reversed(estado.get("messages", []) or []):
        if isinstance(msg, AIMessage):
            return texto_da_resposta(msg.content)
    return ""


# --- Montagem do grafo --------------------------------------------------------
def montar_grafo_redator(checkpointer=None):
    """Monta e compila o grafo do redator."""
    grafo = StateGraph(EstadoColeta)
    grafo.add_node("redator", no_redator)
    grafo.add_node("registrar_relatorio", no_registrar_relatorio)

    grafo.add_edge(START, "redator")
    grafo.add_edge("redator", "registrar_relatorio")
    grafo.add_edge("registrar_relatorio", END)

    return grafo.compile(checkpointer=checkpointer)


grafo_redator = montar_grafo_redator()


def redigir(estado: EstadoColeta) -> dict:
    """Roda o redator sobre um estado que já tem previsão e parecer."""
    return grafo_redator.invoke(estado)


if __name__ == "__main__":
    estado_final = redigir({
        "messages": [],
        "indicador": "ipca",
        "validados": [{"data": "2025-12-01", "valor": 0.41}],
        "avisos": [],
        "previsao": {
            "status": "ok", "valor": 1.60,
            "intervalo": {"minimo": 1.40, "maximo": 1.80, "nivel": 0.95},
            "modelo": "ARIMA(1,0,1)", "observacoes": 36,
        },
        "parecer": {
            "decisao": "rejeita",
            "motivos": ["o valor previsto está 3,2 desvios-padrão acima da média "
                        "dos últimos seis meses sem justificativa econômica"],
            "o_que_corrigir": "tente ARIMA(1,0,0)",
            "confianca": "alta",
        },
        "tentativas": 1,
    })

    print("\n=== RELATÓRIO ===")
    print(estado_final["relatorio"])
