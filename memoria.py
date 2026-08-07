# memoria.py
#
# Memória de DADOS do Agente de Coleta (nível 1): um JSON por indicador em
# data/<indicador>.json. Ao guardar, lê o que já existe e junta os pontos novos
# POR DATA — mesma data sobrescreve (dado revisado ganha), datas novas são
# acrescentadas, nada é duplicado.
#
# O outro nível de memória (estado do grafo via SqliteSaver, por thread_id) fica
# no coletor.py, onde o grafo é compilado.

from __future__ import annotations

import json
from pathlib import Path

from persistencia import escrever_atomico
from tools.schema import PontoSerie

PASTA_DADOS = Path(__file__).parent / "data"


def _caminho(indicador: str) -> Path:
    return PASTA_DADOS / f"{indicador}.json"


def carregar_pontos(indicador: str) -> dict[str, float]:
    """Lê os pontos já persistidos como {data_iso: valor}. Vazio se não existe."""
    caminho = _caminho(indicador)
    if not caminho.exists():
        return {}
    try:
        conteudo = json.loads(caminho.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Arquivo corrompido: não apagamos nem inventamos — tratamos como vazio
        # e o merge subsequente reescreve com o que for válido agora.
        return {}
    return {p["data"]: float(p["valor"]) for p in conteudo.get("pontos", [])}


def salvar_pontos(indicador: str, novos: list[PontoSerie]) -> dict:
    """Junta `novos` ao JSON do indicador por data e persiste.

    Merge por data: a chave é a data ISO; ponto novo com data já existente
    sobrescreve o antigo (assumimos que o dado mais recente é a revisão correta),
    data inédita é acrescentada. Nunca duplica.

    Returns:
        dict com um resumo do merge (quantos já havia, novos, atualizados, total).
    """
    PASTA_DADOS.mkdir(parents=True, exist_ok=True)

    existentes = carregar_pontos(indicador)
    antes = len(existentes)

    novos_count = atualizados_count = 0
    for p in novos:
        chave = p.data.isoformat()
        if chave in existentes:
            if existentes[chave] != p.valor:
                atualizados_count += 1
        else:
            novos_count += 1
        existentes[chave] = p.valor  # repetido sobrescreve, não duplica

    # Persiste ordenado por data — arquivo estável e fácil de auditar.
    # Escrita ATÔMICA: no GitHub Actions o processo pode morrer no meio, e um
    # JSON truncado aqui derrubaria a coleta da rodada seguinte.
    pontos_ordenados = [
        {"data": data, "valor": valor}
        for data, valor in sorted(existentes.items())
    ]
    escrever_atomico(
        _caminho(indicador),
        json.dumps({"indicador": indicador, "pontos": pontos_ordenados},
                   ensure_ascii=False, indent=2),
    )

    return {
        "indicador": indicador,
        "antes": antes,
        "novos": novos_count,
        "atualizados": atualizados_count,
        "total": len(existentes),
    }
