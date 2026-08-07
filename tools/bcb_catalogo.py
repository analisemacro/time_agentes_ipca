# tools/bcb_catalogo.py
#
# Guardrail do BCB: resolver o NOME de uma série do SGS a partir do código.
#
# Por que existe: a API do SGS devolve só (data, valor) — nunca o nome da série.
# Sem um nome conferido, o agente coletaria "números cegos" e poderia rotular a
# coluna errada. Este resolvedor em cascata garante que todo número coletado
# tenha um nome confiável; se não houver, FALHA pedindo o nome ao usuário em vez
# de adivinhar.
#
# Cascata:
#   (1) catálogo curado (nome conferido à mão) — a fonte mais confiável;
#   (2) portal de dados abertos do BCB (CKAN) — reforço best-effort;
#   (3) o nome que o usuário passar explicitamente.
# Se ao fim o nome continuar desconhecido, levanta NomeDeSerieDesconhecido.

from __future__ import annotations

import json
from urllib.request import urlopen

# ---------------------------------------------------------------------------
# (1) Catálogo curado — códigos SGS com nome CONFERIDO À MÃO na fonte oficial.
# É a primeira e mais confiável etapa da cascata. Ampliar aqui é o caminho certo
# para adicionar uma série "de confiança" ao projeto.
# ---------------------------------------------------------------------------
CATALOGO_CURADO: dict[int, str] = {
    433: "IPCA - variação mensal (%)",
    432: "Meta Selic definida pelo Copom (% a.a.)",
    1:   "Câmbio BRL/USD - venda",
}


class NomeDeSerieDesconhecido(Exception):
    """Erro-instrução para o agente: a série não tem nome confiável.

    A mensagem NÃO é um traceback — é o próximo passo que o agente deve tomar:
    pedir ao usuário o nome da série antes de coletar.
    """


def _tentar_ckan(codigo: int) -> str | None:
    """(2) Reforço: tenta o nome no portal de dados abertos do BCB (CKAN).

    Best-effort: o CKAN nem sempre indexa a série pelo código, e a busca textual
    é ruidosa. Só aceitamos o resultado se o dataset for inequivocamente o desta
    série (o slug `{codigo}-sgs` existe e tem título). Em qualquer dúvida,
    devolvemos None e deixamos a cascata seguir — melhor não ter nome do que ter
    um nome errado.
    """
    url = (f"https://dadosabertos.bcb.gov.br/api/3/action/"
           f"package_show?id={codigo}-sgs")
    try:
        with urlopen(url, timeout=15) as resp:
            dados = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if not dados.get("success"):
        return None
    titulo = (dados.get("result") or {}).get("title")
    return titulo.strip() if titulo else None


def resolver_nome_serie(codigo: int, nome_usuario: str | None = None) -> str:
    """Resolve o nome de uma série SGS pela cascata curado → CKAN → usuário.

    Args:
        codigo: código numérico da série no SGS.
        nome_usuario: nome informado explicitamente pelo usuário (opcional).

    Returns:
        O nome resolvido (str).

    Raises:
        NomeDeSerieDesconhecido: se nenhuma etapa produziu um nome e o usuário
            também não forneceu um — a mensagem instrui a pedir o nome.
    """
    # (1) catálogo curado
    if codigo in CATALOGO_CURADO:
        return CATALOGO_CURADO[codigo]

    # (2) CKAN como reforço
    nome_ckan = _tentar_ckan(codigo)
    if nome_ckan:
        return nome_ckan

    # (3) nome fornecido pelo usuário
    if nome_usuario and nome_usuario.strip():
        return nome_usuario.strip()

    # Nada resolveu — falha pedindo o nome, NUNCA coleta cego.
    raise NomeDeSerieDesconhecido(
        f"Não foi possível identificar o nome da série SGS {codigo}: ela não "
        f"está no catálogo curado nem no portal de dados abertos do BCB. "
        f"PRÓXIMO PASSO: peça ao usuário o nome desta série e chame a coleta de "
        f"novo passando esse nome explicitamente. Não colete a série sem um nome "
        f"confiável — o número ficaria sem rótulo."
    )
