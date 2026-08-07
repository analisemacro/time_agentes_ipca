# tools/calendario_ibge.py
#
# Calendário de divulgações do IBGE — quando o IPCA sai.
#
# Papel: responder duas perguntas que a sentinela precisa fazer antes de rodar
# qualquer previsão: qual é a PRÓXIMA data de divulgação do IPCA, e de qual mês
# de referência. Código puro, sem LLM: consultar um calendário publicado é
# consulta, não julgamento.
#
# Fonte: API v3 do calendário do IBGE.
#   https://servicodados.ibge.gov.br/api/v3/calendario
#
# A API devolve TODAS as pesquisas do IBGE (mais de 2.400 eventos, paginados),
# então o filtro é a parte que importa.
#
# A ARMADILHA. Existem quatro produtos de nome parecido, e o do IPCA cheio é
# PREFIXO EXATO do nome dos outros dois:
#
#   9256  Índice Nacional de Preços ao Consumidor Amplo            <- IPCA cheio
#   9260  Índice Nacional de Preços ao Consumidor Amplo 15         <- IPCA-15
#   9262  Índice Nacional de Preços ao Consumidor Amplo Especial   <- IPCA-E
#   9258  Índice Nacional de Preços ao Consumidor                  <- INPC
#
# Filtrar por texto ("contém Amplo") captura 9256, 9260 e 9262 de uma vez —
# conferido rodando. Por isso o filtro aqui é por `produto_id` NUMÉRICO e por
# igualdade, nunca por substring de título. Errar aqui não dá erro: dá uma data
# silenciosamente trocada. O IPCA cheio sai por volta do dia 10; o IPCA-15, por
# volta do dia 26. Pegar o errado faz o sistema prever na véspera da divulgação
# errada, uns 16 dias fora do lugar.
#
# Regra de ouro do projeto, aplicada aqui: se a API não responder, o módulo diz
# "não sei" (status "indisponivel") com o motivo. Nunca estima, nunca repete a
# data do mês passado, nunca inventa.

from __future__ import annotations

import gzip
import json
import zlib
from datetime import date, datetime
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

URL_CALENDARIO = "https://servicodados.ibge.gov.br/api/v3/calendario"

# O identificador EXATO do IPCA cheio. É por este número que filtramos.
PRODUTO_IPCA = 9256

# Os vizinhos perigosos. Não entram no filtro; ficam aqui documentados para quem
# for mexer no módulo saber o que está sendo deliberadamente excluído, e para o
# modo de conferência conseguir mostrar a diferença lado a lado.
PRODUTOS_PARECIDOS = {
    9256: "IPCA (cheio)",
    9260: "IPCA-15 (prévia — NÃO é o IPCA cheio)",
    9262: "IPCA-E (especial, trimestral — NÃO é o IPCA cheio)",
    9258: "INPC (outro índice — NÃO é o IPCA cheio)",
}

# tipo_id 1 = "Divulgação de Indicadores", o evento que solta o número. O
# calendário tem outros tipos (coletiva de imprensa, por exemplo) que não
# significam dado novo publicado.
TIPO_DIVULGACAO = 1

MESES_PT = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril",
    5: "maio", 6: "junho", 7: "julho", 8: "agosto",
    9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro",
}


class CalendarioIndisponivel(Exception):
    """A API do IBGE não respondeu ou respondeu algo que não dá para ler.

    Existe para separar "não consegui saber" de "sei que não tem divulgação
    marcada". São situações diferentes e a sentinela reage diferente a cada uma.
    """


def _decodificar(corpo: bytes, cabecalhos) -> str:
    """Devolve o corpo da resposta como texto, descomprimindo se necessário.

    A API do IBGE às vezes responde comprimida mesmo sem pedirmos (visto na
    prática: um `resp.read().decode("utf-8")` cru quebrava de forma
    intermitente, num byte 0x8b — a assinatura do gzip). Como a falha só
    aparecia em algumas chamadas, é o tipo de coisa que passaria no teste e
    quebraria com a sentinela rodando sozinha de madrugada.
    """
    codificacao = (cabecalhos.get("Content-Encoding") or "").lower()
    if codificacao == "gzip" or corpo[:2] == b"\x1f\x8b":
        corpo = gzip.decompress(corpo)
    elif codificacao == "deflate":
        corpo = zlib.decompress(corpo, -zlib.MAX_WBITS)
    return corpo.decode("utf-8")


def _buscar_pagina(pagina: int, de: str, ate: str, timeout: int) -> dict:
    """Baixa uma página do calendário. Erro de rede vira CalendarioIndisponivel."""
    params = urlencode({"qtd": 100, "page": pagina, "de": de, "ate": ate})
    pedido = Request(
        f"{URL_CALENDARIO}?{params}",
        headers={"Accept": "application/json", "Accept-Encoding": "gzip, deflate"},
    )
    try:
        with urlopen(pedido, timeout=timeout) as resp:
            if resp.status != 200:
                raise CalendarioIndisponivel(
                    f"API do calendário do IBGE respondeu HTTP {resp.status}."
                )
            return json.loads(_decodificar(resp.read(), resp.headers))
    except CalendarioIndisponivel:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, zlib.error,
            gzip.BadGzipFile) as erro:
        raise CalendarioIndisponivel(
            f"A API do calendário do IBGE devolveu algo que não dá para ler: {erro}"
        ) from erro
    except (URLError, TimeoutError, OSError) as erro:
        raise CalendarioIndisponivel(
            f"Não foi possível falar com a API do calendário do IBGE: {erro}"
        ) from erro


def _data_iso(evento: dict) -> str | None:
    """Converte 'dd/mm/aaaa hh:mm:ss' para 'aaaa-mm-dd'. None se vier torto."""
    bruto = (evento.get("data_divulgacao") or "").strip()
    if not bruto:
        return None
    try:
        return datetime.strptime(bruto.split(" ")[0], "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def _referencia(evento: dict) -> dict | None:
    """Extrai o mês/ano de referência do evento (o mês a que o dado se refere).

    Atenção à diferença que confunde: o IPCA divulgado em 10/02/2026 é o IPCA de
    JANEIRO de 2026. A data de divulgação e o mês de referência nunca caem no
    mesmo mês.
    """
    mes = evento.get("mes_referencia_inicio")
    ano = evento.get("ano_referencia_inicio")
    if not mes or not ano:
        return None
    return {
        "mes": int(mes),
        "ano": int(ano),
        "rotulo": f"{MESES_PT[int(mes)]} de {int(ano)}",
    }


def divulgacoes_ipca(
    a_partir_de: str | None = None,
    ate: str | None = None,
    quantas: int = 6,
    timeout: int = 30,
) -> list[dict]:
    """Lista as próximas divulgações do IPCA CHEIO, em ordem cronológica.

    Args:
        a_partir_de: data ISO inicial. Padrão: hoje.
        ate: data ISO final da janela consultada. Padrão: fim do ano que vem —
            o IBGE publica o calendário com bastante antecedência, e uma janela
            larga garante que sempre haja evento futuro suficiente.
        quantas: quantos eventos devolver.
        timeout: segundos por requisição.

    Returns:
        Lista de dicts serializáveis (o estado do grafo vai para o checkpoint
        SQLite, então nada de objeto date cru):
            {"data_divulgacao": "2026-09-09",
             "referencia": {"mes": 8, "ano": 2026, "rotulo": "agosto de 2026"},
             "produto_id": 9256, "titulo": "..."}

    Raises:
        CalendarioIndisponivel: se a API não responder. Quem chama decide o que
            fazer; o módulo não inventa data.
    """
    inicio = a_partir_de or date.today().isoformat()
    fim = ate or date(date.fromisoformat(inicio).year + 2, 12, 31).isoformat()

    achados: list[dict] = []
    pagina, total_paginas = 1, 1

    while pagina <= total_paginas:
        dados = _buscar_pagina(pagina, inicio, fim, timeout)
        total_paginas = int(dados.get("totalPages") or 1)

        for evento in dados.get("items", []):
            # O filtro que importa: igualdade de produto_id, não substring de
            # título. Ver a nota da armadilha no topo do arquivo.
            if evento.get("produto_id") != PRODUTO_IPCA:
                continue
            if evento.get("tipo_id") != TIPO_DIVULGACAO:
                continue

            quando = _data_iso(evento)
            if quando is None or quando < inicio:
                continue

            achados.append({
                "data_divulgacao": quando,
                "referencia": _referencia(evento),
                "produto_id": evento["produto_id"],
                "titulo": (evento.get("nome_produto") or "").strip(),
            })

        pagina += 1

    achados.sort(key=lambda e: e["data_divulgacao"])

    # Defesa contra o mesmo evento aparecer repetido em páginas diferentes.
    unicos: list[dict] = []
    vistos: set[str] = set()
    for evento in achados:
        if evento["data_divulgacao"] in vistos:
            continue
        vistos.add(evento["data_divulgacao"])
        unicos.append(evento)

    return unicos[:quantas]


def proxima_divulgacao_ipca(hoje: str | None = None, timeout: int = 30) -> dict:
    """Responde: quando sai o próximo IPCA, e de qual mês de referência?

    É a função que a sentinela chama. Nunca levanta exceção por falha de rede:
    devolve um dict com `status` dizendo o que aconteceu, porque "não sei" é uma
    resposta legítima e precisa trafegar pelo estado do grafo como dado.

    Returns:
        status "ok":
            {"status": "ok", "data_divulgacao": "2026-09-09", "dias_ate": 34,
             "referencia": {...}, "produto_id": 9256, "consultado_em": "..."}
        status "sem_divulgacao": a API respondeu, mas não há evento futuro
            marcado para o IPCA na janela consultada.
        status "indisponivel": a API não respondeu. Traz `motivo` e NÃO traz
            data — não há data para trazer, e chutar uma seria pior que falhar.
    """
    referencia_hoje = hoje or date.today().isoformat()

    try:
        eventos = divulgacoes_ipca(a_partir_de=referencia_hoje, quantas=1,
                                   timeout=timeout)
    except CalendarioIndisponivel as erro:
        return {
            "status": "indisponivel",
            "motivo": str(erro),
            "consultado_em": referencia_hoje,
        }

    if not eventos:
        return {
            "status": "sem_divulgacao",
            "motivo": ("A API respondeu, mas não há divulgação futura do IPCA "
                       "marcada na janela consultada."),
            "consultado_em": referencia_hoje,
        }

    proximo = eventos[0]
    faltam = (date.fromisoformat(proximo["data_divulgacao"])
              - date.fromisoformat(referencia_hoje)).days

    return {
        "status": "ok",
        "data_divulgacao": proximo["data_divulgacao"],
        "dias_ate": faltam,
        "referencia": proximo["referencia"],
        "produto_id": proximo["produto_id"],
        "consultado_em": referencia_hoje,
    }


def conferir_variantes(timeout: int = 30) -> dict:
    """Modo de conferência: mostra o IPCA cheio ao lado das variantes que enganam.

    Não é usado pelo sistema em produção. Serve para provar, com o dado na tela,
    que o filtro pegou o produto certo — e para flagrar na hora se o IBGE mudar
    algum identificador.
    """
    inicio = date.today().isoformat()
    fim = date(date.today().year + 2, 12, 31).isoformat()

    por_produto: dict[int, list[dict]] = {pid: [] for pid in PRODUTOS_PARECIDOS}
    titulos: dict[int, str] = {}
    capturados_por_texto: set[int] = set()

    pagina, total_paginas = 1, 1
    while pagina <= total_paginas:
        dados = _buscar_pagina(pagina, inicio, fim, timeout)
        total_paginas = int(dados.get("totalPages") or 1)
        for evento in dados.get("items", []):
            pid = evento.get("produto_id")
            # Simula o filtro ingênuo, para mostrar o estrago que ele faria.
            if "Amplo" in (evento.get("nome_produto") or ""):
                capturados_por_texto.add(pid)
            if pid in por_produto and evento.get("tipo_id") == TIPO_DIVULGACAO:
                quando = _data_iso(evento)
                if quando:
                    titulos[pid] = (evento.get("nome_produto") or "").strip()
                    por_produto[pid].append(
                        {"data_divulgacao": quando, "referencia": _referencia(evento)}
                    )
        pagina += 1

    for lista in por_produto.values():
        lista.sort(key=lambda e: e["data_divulgacao"])

    return {
        "produtos": {
            pid: {
                "rotulo": PRODUTOS_PARECIDOS[pid],
                "titulo_na_api": titulos.get(pid, "(nenhum evento na janela)"),
                "eh_o_que_usamos": pid == PRODUTO_IPCA,
                "proximas": lista[:3],
            }
            for pid, lista in por_produto.items()
        },
        "filtro_ingenuo_por_texto_capturaria": sorted(capturados_por_texto),
    }


if __name__ == "__main__":
    print("=" * 72)
    print("CONFERÊNCIA — o filtro pegou o produto certo?")
    print("=" * 72)
    try:
        conferencia = conferir_variantes()
    except CalendarioIndisponivel as erro:
        print(f"NÃO SEI: {erro}")
        raise SystemExit(1)

    for pid, info in sorted(conferencia["produtos"].items()):
        marca = "-->" if info["eh_o_que_usamos"] else "   "
        print(f"\n{marca} produto_id {pid} — {info['rotulo']}")
        print(f"    título na API: {info['titulo_na_api']}")
        for ev in info["proximas"]:
            ref = ev["referencia"]["rotulo"] if ev["referencia"] else "?"
            print(f"      {ev['data_divulgacao']}  (referência: {ref})")

    ingenuo = conferencia["filtro_ingenuo_por_texto_capturaria"]
    print(f"\nSe filtrássemos por título contendo 'Amplo', viriam: {ingenuo}")
    print("Por isso o filtro é por produto_id numérico, e por igualdade.")

    print("\n" + "=" * 72)
    print(f"PRÓXIMAS 6 DIVULGAÇÕES DO IPCA CHEIO (produto_id {PRODUTO_IPCA})")
    print("=" * 72)
    try:
        for ev in divulgacoes_ipca(quantas=6):
            ref = ev["referencia"]["rotulo"] if ev["referencia"] else "?"
            print(f"  {ev['data_divulgacao']}   referência: {ref}")
    except CalendarioIndisponivel as erro:
        print(f"NÃO SEI: {erro}")
        raise SystemExit(1)

    print("\n" + "=" * 72)
    print("O QUE A SENTINELA VAI PERGUNTAR")
    print("=" * 72)
    resposta = proxima_divulgacao_ipca()
    if resposta["status"] == "ok":
        print(f"  Próxima divulgação: {resposta['data_divulgacao']} "
              f"(em {resposta['dias_ate']} dias)")
        print(f"  Mês de referência:  {resposta['referencia']['rotulo']}")
    else:
        print(f"  {resposta['status'].upper()}: {resposta['motivo']}")
