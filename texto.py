# texto.py
#
# Utilitário: extrair o texto de uma resposta do LLM.
#
# Por que existe: as versões recentes do langchain-google-genai devolvem
# `mensagem.content` como LISTA DE BLOCOS
#
#     [{"type": "text", "text": "...", "extras": {...}}]
#
# e não como string. Quem fizer `str(content)` acaba com o repr do Python
# inteiro ("[{'type': 'text', ...}]") no lugar do texto — o que quebra o parsing
# do JSON do crítico e vaza colchetes para dentro do relatório.
#
# Esta função aceita os dois formatos e devolve sempre uma string limpa.

from __future__ import annotations


def texto_da_resposta(conteudo) -> str:
    """Devolve o texto de `mensagem.content`, seja ele string ou lista de blocos.

    Args:
        conteudo: o `.content` de uma mensagem do LangChain — string, lista de
            blocos ({"type": "text", "text": ...}) ou qualquer outra coisa.

    Returns:
        O texto concatenado. String vazia se não houver texto aproveitável.
    """
    if conteudo is None:
        return ""

    if isinstance(conteudo, str):
        return conteudo

    if isinstance(conteudo, list):
        partes: list[str] = []
        for bloco in conteudo:
            if isinstance(bloco, str):
                partes.append(bloco)
            elif isinstance(bloco, dict):
                # Bloco de texto padrão do LangChain.
                if isinstance(bloco.get("text"), str):
                    partes.append(bloco["text"])
        return "".join(partes)

    # Formato inesperado: não forçamos str() aqui para não devolver um repr de
    # objeto como se fosse texto do modelo.
    return ""
