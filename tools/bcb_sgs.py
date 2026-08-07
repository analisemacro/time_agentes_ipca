# tools/bcb_sgs.py
#
# Tool: coleta de séries do BCB/SGS (Sistema Gerenciador de Séries Temporais).
#
# Papel: baixar uma série do SGS pelo código (ou por um atalho amigável).
# Forma direta via biblioteca python-bcb; se ela falhar, cai numa reserva que
# bate direto na API HTTP do BCB. Devolve dado real, sem imputar.

from __future__ import annotations

import json
from urllib.request import urlopen

from langchain_core.tools import tool

from tools.bcb_catalogo import resolver_nome_serie

# Atalhos amigáveis: o agente pode pedir "ipca" em vez de decorar o código 433.
ATALHOS_SGS = {
    "ipca": 433,    # IPCA – variação mensal (%)
    "selic": 432,   # Meta Selic definida pelo Copom (% a.a.)
    "cambio": 1,    # Câmbio BRL/USD – venda
}


def _resolver_codigo(serie: str | int) -> int:
    """Traduz um atalho ('ipca') ou código cru ('433'/433) num código SGS int."""
    if isinstance(serie, str):
        chave = serie.strip().lower()
        if chave in ATALHOS_SGS:
            return ATALHOS_SGS[chave]
        if chave.isdigit():
            return int(chave)
        raise ValueError(
            f"Série '{serie}' não é um atalho conhecido {list(ATALHOS_SGS)} "
            f"nem um código numérico do SGS."
        )
    return int(serie)


def _coleta_via_python_bcb(codigo: int, inicio: str) -> list[dict]:
    """Forma direta: usa a biblioteca python-bcb (bcb.sgs.get)."""
    from bcb import sgs

    df = sgs.get({"valor": codigo}, start=inicio)
    return [
        {"data": idx.strftime("%Y-%m-%d"), "valor": float(v)}
        for idx, v in df["valor"].dropna().items()
    ]


def _coleta_via_http(codigo: int, inicio: str) -> list[dict]:
    """Reserva: bate direto na API HTTP do BCB e converte a vírgula decimal.

    O BCB devolve o valor como texto com vírgula ("0,26"); convertemos para
    ponto ("0.26") antes de virar float.
    """
    # A API HTTP do BCB espera a data como dd/mm/aaaa (a python-bcb aceita ISO,
    # mas aqui falamos direto com o endpoint).
    ano, mes, dia = inicio.split("-")
    inicio_br = f"{dia}/{mes}/{ano}"
    url = (f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"
           f"?formato=json&dataInicial={inicio_br}")
    with urlopen(url, timeout=30) as resp:
        registros = json.loads(resp.read().decode("utf-8"))

    pontos = []
    for r in registros:
        # data vem como "dd/mm/aaaa"; padronizamos para "aaaa-mm-dd".
        dia, mes, ano = r["data"].split("/")
        valor = float(r["valor"].replace(",", "."))  # "0,26" -> "0.26"
        pontos.append({"data": f"{ano}-{mes}-{dia}", "valor": valor})
    return pontos


@tool
def coletar_serie_sgs(serie: str, data_inicio: str = "2004-01-01",
                      nome: str | None = None) -> dict:
    """Coleta uma série temporal do Banco Central do Brasil (SGS).

    Use esta ferramenta para SÉRIES DE TEMPO agregadas do BCB — juros (Selic),
    câmbio (BRL/USD) e inflação cheia (IPCA, IGP-M), além de qualquer outra
    série do SGS pelo seu código numérico.

    NÃO use esta ferramenta para grupos/subitens do IPCA (alimentação, transporte,
    habitação etc.) nem para tabelas desagregadas do IBGE — para isso use a
    ferramenta da SIDRA (coletar_sidra).

    GUARDRAIL: a API do SGS não informa o nome da série. Antes de coletar, a tool
    resolve o nome (catálogo curado → portal de dados abertos → nome que você
    passar). Se a série não estiver nos catálogos e você NÃO passar `nome`, a tool
    falha e pede o nome — não colete um número sem rótulo. Ao coletar uma série
    fora dos atalhos conhecidos, forneça `nome`.

    Args:
        serie: um atalho amigável ("ipca", "selic", "cambio") OU o código cru
            de qualquer série do SGS como texto (ex.: "433", "189", "24364").
        data_inicio: data inicial no formato "AAAA-MM-DD" (padrão: "2004-01-01").
        nome: nome da série, se conhecido. Obrigatório para séries que não estão
            nos catálogos — caso contrário a coleta é recusada.

    Returns:
        dict com o código, o nome resolvido e a lista de pontos:
        {"codigo": 433, "nome": "IPCA - variação mensal (%)", "fonte": "SGS",
        "pontos": [{"data": "2004-01-01", "valor": 0.76}, ...]}. Os valores são
        REAIS, vindos da API do BCB.
    """
    codigo = _resolver_codigo(serie)
    # GUARDRAIL: resolve (ou exige) o nome ANTES de coletar. Se desconhecido e
    # sem nome do usuário, levanta NomeDeSerieDesconhecido com o próximo passo.
    nome_resolvido = resolver_nome_serie(codigo, nome_usuario=nome)
    try:
        pontos = _coleta_via_python_bcb(codigo, data_inicio)
        origem = "python-bcb"
    except Exception:
        # A biblioteca falhou (indisponível, mudança de API, etc.) — cai para a
        # reserva HTTP em vez de derrubar a coleta.
        pontos = _coleta_via_http(codigo, data_inicio)
        origem = "http"

    return {"codigo": codigo, "nome": nome_resolvido, "fonte": "SGS",
            "origem": origem, "pontos": pontos}
