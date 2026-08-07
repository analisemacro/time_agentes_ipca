# conversa.py
#
# O agente que responde perguntas sobre o que o sistema produziu.
#
# Papel: dar uma porta de entrada em linguagem natural ao que já está nos
# arquivos — a série, a previsão da última rodada e o histórico de acerto. É o
# único ponto do dashboard que chama LLM.
#
# A DIFERENÇA PARA OS OUTROS AGENTES. O time (coletor, previsor, crítico,
# redator) PRODUZ o resultado. Este aqui só EXPLICA um resultado que já existe.
# Ele não tem tools, não coleta nada, não roda modelo estatístico e não escreve
# em arquivo nenhum. Tudo o que ele sabe cabe no contexto que montamos aqui.
#
# É de propósito: um agente de conversa com acesso a tools poderia disparar
# coleta a cada pergunta, e o dashboard deixaria de ser barato. Aqui o custo de
# uma pergunta é uma chamada de LLM, e só.
#
# A REGRA ANTI-INVENÇÃO. A mesma do sistema inteiro: o agente nunca inventa
# número. Aqui ela é mais delicada que no resto do projeto, porque um LLM
# "sabe" a Selic, o câmbio e o IPCA de anos que não estão na nossa série — e
# responderia com naturalidade. Três camadas seguram isso:
#
#   1. o prompt (prompts/conversa.md) manda dizer "não sei" e dá exemplos;
#   2. o contexto declara explicitamente os LIMITES dos dados (o que existe, de
#      quando até quando), para o modelo ter como perceber que a pergunta caiu
#      fora;
#   3. temperature=0 — não queremos criatividade em cima de número.
#
# Nenhuma das três é garantia sozinha. Juntas, tornam a recusa o caminho fácil.

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

# De onde vem a chave, nos dois lugares onde este código roda:
#
#   - na sua máquina: do arquivo .env (que NÃO vai para o repositório);
#   - no servidor (Posit Connect / GitHub Actions): de variável de ambiente.
#
# `load_dotenv()` cobre os dois casos sozinho: se não existe .env, ele devolve
# False em silêncio e a variável de ambiente que já estiver definida continua
# valendo. E quando existe, ele NÃO sobrescreve variável já definida — então o
# segredo do servidor sempre ganha do arquivo local.
load_dotenv()

PASTA = Path(__file__).parent
PROMPT = (PASTA / "prompts" / "conversa.md").read_text(encoding="utf-8")

# Quantos meses da série vão no contexto.
#
# 24 é o meio-termo: dá para ver tendência e sazonalidade sem despejar 20 anos de
# série num prompt (caro, lento, e o modelo se perde). Perguntas sobre um mês
# fora dessa janela devem receber "não sei" — o contexto declara o recorte, e é
# por isso que ele precisa estar escrito lá.
MESES_NO_CONTEXTO = 24


class ChaveAusente(Exception):
    """Não há GOOGLE_API_KEY no ambiente.

    Existe para dar uma mensagem clara em vez do ValidationError críptico que a
    biblioteca levanta. É a falha mais provável logo depois de publicar no
    servidor — a chave é a única coisa que não vem do repositório.
    """


def _llm():
    """Cria o modelo. Importado aqui dentro para o dashboard abrir sem carregar
    a stack do Gemini — quem não usa o chat não paga o import."""
    import os

    if not os.environ.get("GOOGLE_API_KEY"):
        raise ChaveAusente(
            "A variável de ambiente GOOGLE_API_KEY não está definida. "
            "No Posit Connect Cloud, cadastre-a em Settings > Variables do "
            "conteúdo publicado. Na sua máquina, ela vem do arquivo .env "
            "(que não vai para o repositório)."
        )

    from langchain_google_genai import ChatGoogleGenerativeAI

    # temperature=0: a resposta deve ser a leitura mais direta possível dos
    # dados. Criatividade aqui vira número inventado.
    return ChatGoogleGenerativeAI(model="gemini-flash-lite-latest", temperature=0)


def montar_contexto(resultado: dict | None, historico: list[dict]) -> str:
    """Monta o texto com TUDO que o agente pode saber.

    Declara os limites de propósito: o que existe, de quando até quando, e o que
    o sistema NÃO tem. Sem isso o modelo não tem como distinguir "não está no
    contexto" de "não perguntaram ainda", e a recusa vira sorte.
    """
    partes: list[str] = []

    partes.append(
        "# DADOS DISPONÍVEIS\n\n"
        "Este é TODO o conteúdo a que você tem acesso. Não há mais nada."
    )

    # --- o que o sistema NÃO tem, dito antes do que ele tem ---
    partes.append(
        "## Limites (o que este sistema NÃO acompanha)\n\n"
        "- Apenas o IPCA. Não há Selic, câmbio, PIB, desemprego nem qualquer "
        "outro indicador.\n"
        "- Apenas o Brasil.\n"
        "- Não há Relatório Focus nem projeção de banco ou consultoria.\n"
        "- Não há dado de subitens/grupos do IPCA (alimentação, transporte "
        "etc.), só o índice cheio.\n"
        "- Não há acesso a notícias, à internet ou a qualquer API neste "
        "momento.\n\n"
        "Perguntas sobre qualquer um desses itens devem ser respondidas com "
        "'não sei / não está no sistema'."
    )

    if resultado is None:
        partes.append(
            "## Situação atual\n\n"
            "NENHUMA rodada foi publicada ainda. Não há previsão, série "
            "publicada nem relatório. Se perguntarem sobre a previsão atual, "
            "explique que o sistema ainda não rodou pela primeira vez."
        )
    else:
        serie = resultado.get("serie") or []
        recorte = serie[-MESES_NO_CONTEXTO:]
        previsao = resultado.get("previsao") or {}
        parecer = resultado.get("parecer") or {}

        partes.append(
            f"## Série do IPCA (variação % mensal)\n\n"
            f"A série completa do sistema tem {len(serie)} meses, de "
            f"{serie[0]['data']} a {serie[-1]['data']}.\n"
            f"Abaixo estão os últimos {len(recorte)} meses. Meses fora deste "
            f"recorte você NÃO tem — se perguntarem sobre eles, diga que não "
            f"estão no contexto.\n\n"
            + "\n".join(f"- {p['data'][:7]}: {p['valor']:.2f}%" for p in recorte)
        )

        aprovado = parecer.get("aprovado")
        partes.append(
            f"## Previsão da última rodada\n\n"
            f"- Rodada em: {resultado.get('rodada_em')}\n"
            f"- Valor previsto: {previsao.get('valor')}% "
            f"(para o mês seguinte ao fim da série)\n"
            f"- Intervalo: {previsao.get('intervalo_min')}% a "
            f"{previsao.get('intervalo_max')}% "
            f"(nível {previsao.get('intervalo_nivel')})\n"
            f"- Modelo: {previsao.get('modelo')}\n"
            f"- Parecer do crítico: "
            f"{'APROVADO' if aprovado else 'NÃO APROVADO'}"
            + (f"\n- Motivos do crítico: "
               f"{'; '.join(parecer.get('motivos') or [])}"
               if parecer.get("motivos") else "")
        )

        if not aprovado:
            partes.append(
                "ATENÇÃO: a previsão acima NÃO foi aprovada pelo crítico. "
                "Sempre que citar esse número, mencione que ele foi recusado e "
                "por quê."
            )

        if resultado.get("relatorio"):
            partes.append(f"## Relatório escrito pelo redator\n\n"
                          f"{resultado['relatorio']}")

    # --- histórico de acerto ---
    if historico:
        linhas = []
        for h in historico:
            real = h.get("valor_real")
            if real:
                linhas.append(
                    f"- {h['referencia']}: previu {h['valor_previsto']}%, "
                    f"saiu {real}%, erro {h.get('erro')} p.p. "
                    f"(origem: {h.get('origem') or 'não registrada'})"
                )
            else:
                linhas.append(
                    f"- {h['referencia']}: previu {h['valor_previsto']}%, "
                    f"ainda NÃO conferido (o IBGE ainda não publicou este mês)"
                )
        partes.append(
            "## Histórico de previsões deste sistema\n\n"
            "Erro = previsto menos realizado. Positivo quer dizer que o sistema "
            "previu inflação MAIOR do que a que veio.\n\n" + "\n".join(linhas)
        )
    else:
        partes.append("## Histórico de previsões\n\n"
                      "Nenhuma previsão registrada ainda.")

    return "\n\n".join(partes)


def _texto_da_resposta(resposta) -> str:
    """Extrai o texto de uma resposta do Gemini.

    O `.content` nem sempre é string: nas versões recentes do
    langchain-google-genai ele vem como lista de "partes"
    ([{"type": "text", "text": "..."}]). Um `.content.strip()` cru quebra com
    AttributeError — foi o que aconteceu no primeiro teste deste módulo.
    """
    conteudo = getattr(resposta, "content", resposta)

    if isinstance(conteudo, str):
        return conteudo.strip()

    if isinstance(conteudo, list):
        pedacos = []
        for parte in conteudo:
            if isinstance(parte, str):
                pedacos.append(parte)
            elif isinstance(parte, dict):
                pedacos.append(parte.get("text") or parte.get("content") or "")
        return "\n".join(p for p in pedacos if p).strip()

    return str(conteudo).strip()


def responder(pergunta: str, resultado: dict | None, historico: list[dict],
              conversa_anterior: list[dict] | None = None) -> str:
    """Responde a uma pergunta com base apenas no contexto montado.

    Args:
        pergunta: o que o usuário escreveu.
        resultado: o dict de data/resultado.json (ou None).
        historico: as linhas de data/previsoes.csv.
        conversa_anterior: as mensagens já trocadas, como
            [{"role": "user"|"assistant", "content": "..."}], para o agente
            entender "e no mês anterior?" sem repetir a pergunta inteira.

    Returns:
        O texto da resposta. Falha de API vira uma mensagem explicando — o
        dashboard não deve quebrar porque a cota acabou.
    """
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    mensagens = [
        SystemMessage(content=PROMPT),
        SystemMessage(content=montar_contexto(resultado, historico)),
    ]

    # Só as últimas trocas: o contexto já é grande, e conversa longa demais
    # empurra os dados para longe da pergunta.
    for msg in (conversa_anterior or [])[-6:]:
        if msg["role"] == "user":
            mensagens.append(HumanMessage(content=msg["content"]))
        else:
            mensagens.append(AIMessage(content=msg["content"]))

    mensagens.append(HumanMessage(content=pergunta))

    try:
        return _texto_da_resposta(_llm().invoke(mensagens))
    except ChaveAusente as erro:
        # Separado do resto: a causa é configuração, não rede. Confundir os dois
        # manda quem publicou caçar problema de conexão que não existe.
        return f"⚙️ **Configuração faltando.** {erro}"
    except Exception as erro:
        # Cota estourada, rede fora, chave inválida. A mensagem é a resposta —
        # e deixa claro que NÃO é uma resposta sobre os dados.
        return (f"Não consegui responder agora: a chamada ao modelo falhou "
                f"(`{type(erro).__name__}`). Isto é um problema de conexão ou "
                f"de cota da API, não uma resposta sobre os dados. "
                f"Detalhe: {erro}")


if __name__ == "__main__":
    import sys

    from publicacao import ler_resultado
    from registro import historico as ler_historico

    pergunta = " ".join(sys.argv[1:]) or "Qual é a previsão atual e ela foi aprovada?"
    print(f"PERGUNTA: {pergunta}\n")
    print(responder(pergunta, ler_resultado(), ler_historico()))
