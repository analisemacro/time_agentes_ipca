# tools/ibge_sidra.py
#
# Tools: coleta e descrição de tabelas do IBGE/SIDRA (via sidrapy).
#
# Papel: baixar séries desagregadas do SIDRA (ex.: grupos do IPCA) e ler os
# metadados de uma tabela para o agente saber quais variáveis/classificações
# existem antes de coletar. Padroniza a saída no mesmo formato das demais tools.

from __future__ import annotations

import gzip
import json
from urllib.request import Request, urlopen

import re

import sidrapy
from langchain_core.tools import tool

_PADRAO_MES = re.compile(r"^(19|20)\d{4}$")  # AAAAMM


class CombinacaoSidraInvalida(Exception):
    """Erro-instrução para o agente: variável/classificação/categoria não existe
    na tabela. A mensagem lista o que é válido e manda ver o cardápio — não é
    um traceback."""


def _metadados_sidra(tabela: str) -> dict:
    """Baixa e decodifica os metadados (gzip) de uma tabela do SIDRA."""
    url = f"https://servicodados.ibge.gov.br/api/v3/agregados/{tabela}/metadados"
    req = Request(url, headers={"Accept-Encoding": "gzip"})
    with urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _validar_combinacao(meta: dict, tabela: str, variavel: str,
                        classificacao: str | None, categoria: str | None) -> None:
    """GUARDRAIL: confere variável/classificação/categoria contra os metadados.

    Sem isso, a SIDRA aceita uma combinação inexistente e devolve silenciosamente
    o agregado — o agente coletaria a série ERRADA achando que é o grupo pedido.
    """
    ids_var = {str(v["id"]) for v in meta.get("variaveis", [])}
    if str(variavel) not in ids_var:
        validas = ", ".join(f'{v["id"]}={v["nome"]}' for v in meta.get("variaveis", []))
        raise CombinacaoSidraInvalida(
            f"Variável {variavel} não existe na tabela {tabela}. "
            f"Variáveis válidas: {validas}. "
            f"PRÓXIMO PASSO: escolha uma variável válida ou chame "
            f"descrever_tabela_sidra('{tabela}') para ver o cardápio completo."
        )

    if classificacao is None and categoria is None:
        return  # coleta agregada, sem grupo — nada a validar aqui

    if (classificacao is None) != (categoria is None):
        raise CombinacaoSidraInvalida(
            "Passe classificação E categoria juntas (ou nenhuma das duas). "
            f"PRÓXIMO PASSO: chame descrever_tabela_sidra('{tabela}') para ver "
            f"as classificações e suas categorias."
        )

    classifs = {str(c["id"]): c for c in meta.get("classificacoes", [])}
    if str(classificacao) not in classifs:
        validas = ", ".join(f'{c["id"]}={c["nome"]}' for c in meta.get("classificacoes", []))
        raise CombinacaoSidraInvalida(
            f"Classificação {classificacao} não existe na tabela {tabela}. "
            f"Classificações válidas: {validas or '(nenhuma)'}. "
            f"PRÓXIMO PASSO: chame descrever_tabela_sidra('{tabela}')."
        )

    cats_validas = {str(cat["id"]): cat["nome"]
                    for cat in classifs[str(classificacao)].get("categorias", [])}
    if str(categoria) not in cats_validas:
        amostra = ", ".join(f"{cid}={nome}" for cid, nome in list(cats_validas.items())[:8])
        raise CombinacaoSidraInvalida(
            f"Categoria {categoria} não existe na classificação {classificacao} "
            f"da tabela {tabela}. Exemplos de categorias válidas: {amostra}... "
            f"PRÓXIMO PASSO: chame descrever_tabela_sidra('{tabela}') para a lista "
            f"completa e escolha a categoria certa (não colete o agregado por engano)."
        )


def _achar_coluna_periodo(df) -> str:
    """Devolve o nome da coluna de dimensão cujo conteúdo é um mês AAAAMM.

    O sidrapy nomeia as dimensões por ordem (D1C, D2C, ...), que varia entre
    consultas; identificamos a coluna do período pelo formato dos valores.
    """
    for col in [c for c in df.columns if c.endswith("C")]:
        amostra = df[col].dropna().astype(str)
        if not amostra.empty and _PADRAO_MES.match(amostra.iloc[0]):
            return col
    raise ValueError("Não encontrei a coluna de período (AAAAMM) no retorno do SIDRA.")


@tool
def coletar_sidra(
    tabela: str = "1737",
    variavel: str = "63",
    classificacao: str | None = None,
    categoria: str | None = None,
    periodo: str = "all",
) -> dict:
    """Coleta uma série desagregada do IBGE via SIDRA (grupos e subitens).

    Use esta ferramenta quando precisar de dados DESAGREGADOS do IBGE que não
    existem como série pronta no SGS — em especial os GRUPOS e subitens do IPCA
    (alimentação e bebidas, transportes, habitação etc.), obtidos combinando uma
    classificação e uma categoria da tabela.

    Para a inflação cheia ou séries agregadas de juros/câmbio, prefira a
    ferramenta do Banco Central (coletar_serie_sgs) — é mais direta.

    Se não souber quais variáveis, classificações ou categorias a tabela oferece,
    chame antes a ferramenta descrever_tabela_sidra.

    GUARDRAIL: a combinação variável/classificação/categoria é conferida contra os
    metadados da tabela antes da coleta. Se algo não existir, a tool falha listando
    o que é válido — assim você não coleta o agregado achando que é um grupo.

    Args:
        tabela: código da tabela SIDRA (padrão "1737" = IPCA série histórica).
        variavel: código da variável (padrão "63" = IPCA variação mensal).
        classificacao: código da classificação, ex. "315" (grupos), ou None.
        categoria: código da categoria dentro da classificação, ou None.
        periodo: período SIDRA, ex. "all", "last 12" (padrão "all").

    Returns:
        dict {"tabela", "variavel", "fonte": "SIDRA", "pontos": [{"data",
        "valor"}, ...]}. A data vem no código de mês do SIDRA (AAAAMM).
    """
    # GUARDRAIL: valida a combinação contra os metadados ANTES de coletar. Se
    # algo não existe, levanta CombinacaoSidraInvalida em vez de deixar a SIDRA
    # devolver o agregado no lugar do grupo pedido.
    meta = _metadados_sidra(str(tabela))
    _validar_combinacao(meta, str(tabela), str(variavel), classificacao, categoria)

    kwargs = dict(
        table_code=str(tabela),
        territorial_level="1",
        ibge_territorial_code="all",
        variable=str(variavel),
        period=periodo,
    )
    if classificacao is not None and categoria is not None:
        kwargs["classification"] = str(classificacao)
        kwargs["categories"] = str(categoria)

    df = sidrapy.get_table(**kwargs)

    # A primeira linha do retorno do sidrapy é um cabeçalho descritivo, não dado.
    df = df.iloc[1:]

    # A coluna do PERÍODO não tem posição fixa: o sidrapy rotula as dimensões por
    # ordem (D1C, D2C, D3C...), que muda conforme a consulta tenha ou não
    # classificação. Em vez de fixar "D3C", localizamos a coluna cujo conteúdo é
    # um código de mês AAAAMM (6 dígitos começando por 19/20). Isso evita gravar
    # o código da variável no lugar da data.
    col_periodo = _achar_coluna_periodo(df)

    pontos = []
    for _, linha in df.iterrows():
        bruto = linha["V"]  # valor na coluna "V"
        if bruto in ("...", "-", "..", None):  # ausências do IBGE — não inventar
            continue
        pontos.append({"data": linha[col_periodo], "valor": float(bruto)})

    return {"tabela": str(tabela), "variavel": str(variavel),
            "fonte": "SIDRA", "pontos": pontos}


@tool
def descrever_tabela_sidra(tabela: str = "1737") -> dict:
    """Descreve uma tabela do SIDRA: o cardápio de variáveis e classificações.

    Use esta ferramenta ANTES de coletar_sidra quando não souber quais variáveis,
    classificações ou categorias uma tabela oferece. Ela lê os metadados oficiais
    do IBGE e devolve as opções disponíveis, evitando que você adivinhe códigos.

    Args:
        tabela: código da tabela SIDRA (padrão "1737" = IPCA série histórica).

    Returns:
        dict {"tabela", "nome", "periodicidade", "variaveis": [{"id", "nome"}],
        "classificacoes": [{"id", "nome", "categorias": [{"id", "nome"}]}]}.
    """
    meta = _metadados_sidra(str(tabela))

    variaveis = [{"id": v["id"], "nome": v["nome"]}
                 for v in meta.get("variaveis", [])]
    classificacoes = [
        {"id": c["id"], "nome": c["nome"],
         "categorias": [{"id": cat["id"], "nome": cat["nome"]}
                        for cat in c.get("categorias", [])]}
        for c in meta.get("classificacoes", [])
    ]
    return {
        "tabela": str(tabela),
        "nome": meta.get("nome"),
        "periodicidade": meta.get("periodicidade"),
        "variaveis": variaveis,
        "classificacoes": classificacoes,
    }
