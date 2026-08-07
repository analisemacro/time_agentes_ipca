# tools/ultimo_ponto.py
#
# Sonda barata: qual é a DATA do último ponto publicado de uma série do SGS?
#
# Papel: responder "a série andou?" sem baixar a série inteira. A sentinela roda
# todo dia; baixar 20 anos de IPCA para olhar só a última linha seria desperdício
# diário de banda e de tempo.
#
# O SGS tem um endpoint próprio para isso:
#   .../bcdata.sgs.{codigo}/dados/ultimos/{n}?formato=json
#
# Medido na prática contra o IPCA (série 433): 38 bytes com `ultimos/1`, contra
# 10.005 bytes da série inteira desde 2004. Cerca de 260 vezes menos dado
# trafegado, para a mesma resposta.
#
# Código puro, sem LLM. Perguntar a data do último ponto é consulta, não
# julgamento.
#
# Regra de ouro aplicada aqui: falha de rede levanta FonteIndisponivel. Quem
# chama NÃO pode confundir "a fonte não respondeu" com "não há dado novo" — as
# duas coisas parecem iguais para quem só olha o resultado, e tratá-las igual
# esconderia uma fonte fora do ar por semanas.

from __future__ import annotations

import gzip
import json
import zlib
from datetime import date
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

URL_SGS = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados/ultimos/{n}"

# Códigos SGS por indicador. Espelha ATALHOS_SGS de tools/bcb_sgs.py; ficam
# repetidos de propósito para a sonda não depender do módulo de coleta (que
# importa langchain e python-bcb — peso que a sentinela não precisa carregar
# para fazer uma pergunta de 38 bytes).
CODIGOS_SGS = {
    "ipca": 433,
    "selic": 432,
    "cambio": 1,
}


class FonteIndisponivel(Exception):
    """A fonte não respondeu, ou respondeu algo ilegível.

    Existe para separar "não consegui verificar" de "verifiquei, não mudou nada".
    """


def _decodificar(corpo: bytes, cabecalhos) -> str:
    """Devolve o corpo como texto, descomprimindo se vier comprimido."""
    codificacao = (cabecalhos.get("Content-Encoding") or "").lower()
    if codificacao == "gzip" or corpo[:2] == b"\x1f\x8b":
        corpo = gzip.decompress(corpo)
    elif codificacao == "deflate":
        corpo = zlib.decompress(corpo, -zlib.MAX_WBITS)
    return corpo.decode("utf-8")


def _resolver_codigo(indicador: str) -> int:
    chave = indicador.strip().lower()
    if chave in CODIGOS_SGS:
        return CODIGOS_SGS[chave]
    if chave.isdigit():
        return int(chave)
    raise ValueError(
        f"Indicador '{indicador}' não é conhecido {list(CODIGOS_SGS)} nem um "
        f"código numérico do SGS."
    )


def ultima_data_publicada(indicador: str = "ipca", timeout: int = 20) -> dict:
    """Data (e valor) do último ponto publicado da série, sem baixá-la inteira.

    Args:
        indicador: "ipca", "selic", "cambio" ou um código cru do SGS.
        timeout: segundos.

    Returns:
        {"indicador": "ipca", "codigo": 433, "data": "2026-06-01",
         "valor": 0.16}

    Raises:
        FonteIndisponivel: rede fora, HTTP != 200, corpo ilegível ou resposta
            vazia. Nunca devolve "sem dado" por falha de rede.
    """
    codigo = _resolver_codigo(indicador)
    url = URL_SGS.format(codigo=codigo, n=1) + "?" + urlencode({"formato": "json"})
    pedido = Request(url, headers={"Accept": "application/json",
                                   "Accept-Encoding": "gzip, deflate"})

    try:
        with urlopen(pedido, timeout=timeout) as resp:
            if resp.status != 200:
                raise FonteIndisponivel(
                    f"SGS respondeu HTTP {resp.status} para a série {codigo}."
                )
            registros = json.loads(_decodificar(resp.read(), resp.headers))
    except FonteIndisponivel:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, zlib.error,
            gzip.BadGzipFile) as erro:
        raise FonteIndisponivel(
            f"SGS devolveu algo que não dá para ler na série {codigo}: {erro}"
        ) from erro
    except (URLError, TimeoutError, OSError) as erro:
        raise FonteIndisponivel(
            f"Não foi possível falar com o SGS (série {codigo}): {erro}"
        ) from erro

    if not registros:
        # Resposta vazia não é "a série não andou": é resposta que não dá para
        # interpretar. Tratar como ausência de dado novo mascararia o problema.
        raise FonteIndisponivel(
            f"SGS respondeu sem nenhum ponto para a série {codigo}."
        )

    ultimo = registros[-1]
    try:
        dia, mes, ano = ultimo["data"].split("/")   # "01/06/2026"
        quando = date(int(ano), int(mes), int(dia)).isoformat()
        valor = float(str(ultimo["valor"]).replace(",", "."))
    except (KeyError, ValueError, AttributeError) as erro:
        raise FonteIndisponivel(
            f"Ponto do SGS em formato inesperado na série {codigo}: "
            f"{ultimo!r} — {erro}"
        ) from erro

    return {"indicador": indicador, "codigo": codigo, "data": quando,
            "valor": valor}


if __name__ == "__main__":
    for ind in ("ipca", "selic", "cambio"):
        try:
            r = ultima_data_publicada(ind)
            print(f"{ind:8} último ponto: {r['data']}  valor: {r['valor']}")
        except FonteIndisponivel as erro:
            print(f"{ind:8} NÃO DEU PARA VERIFICAR: {erro}")
