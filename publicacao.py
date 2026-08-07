# publicacao.py
#
# O contrato entre o job automático e o dashboard.
#
# Papel: no fim de uma rodada bem-sucedida, virar o estado do grafo — que é um
# objeto interno, cheio de mensagens de LLM e detalhe de agente — num arquivo
# que uma página web consegue ler sem saber nada de agentes.
#
# A DIVISÃO QUE IMPORTA. De um lado o job: roda de madrugada, chama LLM, tem
# grafo, laço de crítica, checkpoint. Do outro o dashboard: abre um arquivo e
# desenha. O dashboard NÃO importa supervisor.py, não conhece LangGraph, não sabe
# o que é um nó. Ele lê data/resultado.json e pronto. É por isso que este módulo
# existe: sem ele, a página web precisaria entender o sistema inteiro para
# mostrar um número.
#
# Consequência prática: o formato daqui é uma PROMESSA. Mudar o nome de um campo
# quebra a página. Por isso tem VERSAO_CONTRATO — se um dia o formato mudar de
# verdade, o dashboard consegue perceber em vez de ler lixo achando que entendeu.
#
# ============================================================================
# O QUE É UMA RODADA BEM-SUCEDIDA
# ============================================================================
#
# Cuidado com a armadilha: "bem-sucedida" NÃO quer dizer "o crítico aprovou".
# O supervisor publica o relatório mesmo quando o crítico reprova e o teto de
# tentativas estoura — e isso é de propósito, porque "não aprovado, e por isto"
# é mais útil que silêncio.
#
# Aqui, rodada bem-sucedida = o time produziu um resultado COMPLETO: série,
# previsão com número, parecer e relatório. Uma rodada que morreu no meio (sem
# previsão, sem relatório) não é publicada, e o arquivo bom da rodada anterior
# fica onde está.
#
# Quando o crítico reprovou, o arquivo sai assim mesmo, com `aprovado: false` e
# o motivo em `parecer.motivos`. O dashboard tem obrigação de mostrar isso — e o
# campo `alerta` existe para ele não ter desculpa de não ter visto.
#
# ============================================================================
# GRAVAÇÃO
# ============================================================================
#
# Escrita atômica (persistencia.escrever_atomico): uma rodada que morre no meio
# da escrita NÃO deixa o dashboard lendo meio JSON. Ou fica o arquivo da rodada
# anterior inteiro, ou o novo inteiro. O dashboard nunca vê um estado partido.
#
# Código puro, sem LLM. Formatar dado que já existe é formatação.

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from persistencia import escrever_atomico, ler_json

PASTA_DADOS = Path(__file__).parent / "data"
ARQUIVO_RESULTADO = PASTA_DADOS / "resultado.json"

# A versão do formato. O dashboard deve conferir antes de confiar nos campos.
# Só muda quando o formato muda de um jeito que quebra quem já lê.
VERSAO_CONTRATO = 1


class RodadaIncompleta(Exception):
    """A rodada não produziu resultado publicável.

    Levantada em vez de gravar um arquivo pela metade: o arquivo bom da rodada
    anterior vale mais que um novo incompleto.
    """


def _agora_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def montar_resultado(estado: dict, indicador: str = "ipca",
                     quando: str | None = None) -> dict:
    """Traduz o estado final do grafo no formato do contrato.

    Só lê o estado; não grava nada. Separada de `publicar()` para dar para
    inspecionar (e testar) o que sairia, sem tocar no arquivo.

    Args:
        estado: o dict devolvido por `supervisor.rodar_time()`.
        indicador: qual indicador esta rodada tratou.
        quando: data ISO da rodada. Padrão: hoje.

    Returns:
        O dict do contrato, pronto para virar JSON.

    Raises:
        RodadaIncompleta: se faltar previsão com número ou relatório — sem isso
            não há o que a página mostrar, e publicar seria piorar o que já está
            publicado.
    """
    previsao = estado.get("previsao") or {}
    parecer = estado.get("parecer") or {}
    relatorio = (estado.get("relatorio") or "").strip()
    validados = estado.get("validados") or []

    if previsao.get("status") != "ok" or previsao.get("valor") is None:
        raise RodadaIncompleta(
            f"a rodada não produziu previsão com número "
            f"(status: {previsao.get('status', 'ausente')}) — nada a publicar."
        )
    if not relatorio:
        raise RodadaIncompleta(
            "a rodada não produziu relatório — nada a publicar."
        )
    if not validados:
        raise RodadaIncompleta(
            "a rodada não tem série validada — o gráfico ficaria vazio."
        )

    # A série, já ordenada por data: é o que a página desenha no gráfico. Os
    # pontos vêm como dicts simples {"data", "valor"}, sem objeto nenhum.
    serie = sorted(
        ({"data": str(p["data"]), "valor": float(p["valor"])}
         for p in validados),
        key=lambda p: p["data"],
    )

    aprovado = parecer.get("decisao") == "aprova"
    intervalo = previsao.get("intervalo") or {}
    hoje = quando or date.today().isoformat()

    resultado = {
        # --- metadados do contrato ---
        "versao_contrato": VERSAO_CONTRATO,
        "indicador": indicador,
        "rodada_em": hoje,
        "rodada_em_utc": _agora_utc(),

        # --- a série usada (o gráfico) ---
        "serie": serie,
        "ultima_data_serie": serie[-1]["data"],

        # --- a previsão (o número em destaque) ---
        "previsao": {
            "valor": float(previsao["valor"]),
            "intervalo_min": intervalo.get("minimo"),
            "intervalo_max": intervalo.get("maximo"),
            "intervalo_nivel": intervalo.get("nivel"),
            "modelo": previsao.get("modelo", ""),
            "observacoes": previsao.get("observacoes", ""),
        },

        # --- o parecer do crítico (o selo de qualidade) ---
        "parecer": {
            "aprovado": aprovado,
            "decisao": parecer.get("decisao", "ausente"),
            "motivos": parecer.get("motivos") or [],
            "confianca": parecer.get("confianca", ""),
            # Como o parecer foi produzido: pelo LLM, por regra de código ou por
            # falha de formato. A página pode querer distinguir "o crítico
            # avaliou e reprovou" de "não deu para avaliar".
            "origem": parecer.get("origem", "llm"),
        },

        # --- o texto do redator (o corpo da página) ---
        "relatorio": relatorio,

        # --- avisos acumulados na rodada (transparência) ---
        "avisos": list(estado.get("avisos") or []),
        "tentativas_de_previsao": estado.get("tentativas", 0),
    }

    # O campo que o dashboard não tem como ignorar. Quando o crítico reprovou, o
    # número saiu MESMO ASSIM (por esgotamento de tentativas), e mostrar esse
    # número sem a ressalva seria apresentar como validado algo que foi recusado.
    if not aprovado:
        motivos = resultado["parecer"]["motivos"]
        resultado["alerta"] = {
            "tipo": "previsao_nao_aprovada",
            "texto": ("Esta previsão NÃO foi aprovada pelo crítico. "
                      + (motivos[0] if motivos else "Motivo não registrado.")),
        }
    else:
        resultado["alerta"] = None

    return resultado


def publicar(estado: dict, indicador: str = "ipca",
             quando: str | None = None) -> dict:
    """Grava data/resultado.json a partir do estado final do grafo.

    Escrita atômica: se o processo morrer no meio, o arquivo da rodada anterior
    continua íntegro. O dashboard nunca lê um JSON pela metade.

    Raises:
        RodadaIncompleta: rodada sem resultado publicável. O arquivo anterior
            NÃO é tocado — é a regra que o pedido chama de "não sobrescreve o
            arquivo bom com um pela metade".
    """
    resultado = montar_resultado(estado, indicador=indicador, quando=quando)
    escrever_atomico(
        ARQUIVO_RESULTADO,
        json.dumps(resultado, ensure_ascii=False, indent=2) + "\n",
    )
    return resultado


def publicar_se_completa(estado: dict, indicador: str = "ipca",
                         quando: str | None = None) -> dict:
    """Versão que não explode: publica se der, e explica se não der.

    Para o job automático, que não deve morrer por causa de uma rodada ruim —
    a rodada ruim é informação, não motivo de parada.

    Returns:
        {"publicado": True, "resultado": {...}} ou
        {"publicado": False, "motivo": "...", "arquivo_anterior_preservado": True}
    """
    try:
        resultado = publicar(estado, indicador=indicador, quando=quando)
        return {"publicado": True, "resultado": resultado}
    except RodadaIncompleta as erro:
        return {
            "publicado": False,
            "motivo": str(erro),
            # Dito explicitamente para quem lê o log do job não ficar na dúvida
            # sobre o que aconteceu com o que já estava publicado.
            "arquivo_anterior_preservado": ARQUIVO_RESULTADO.exists(),
        }


# ---------------------------------------------------------------------------
# O lado do dashboard. Fica aqui de propósito: o contrato tem dois lados, e
# escrever os dois no mesmo arquivo evita que eles se desencontrem com o tempo.
# ---------------------------------------------------------------------------
def ler_resultado() -> dict | None:
    """Lê data/resultado.json. None se ainda não houve rodada publicada.

    É o que o dashboard chama. Não importa nada de agentes: lê um JSON e devolve
    um dict. Arquivo ausente ou ilegível devolve None — a página mostra "nenhuma
    rodada publicada ainda" em vez de quebrar.
    """
    if not ARQUIVO_RESULTADO.exists():
        return None
    dados = ler_json(ARQUIVO_RESULTADO, padrao={})
    if not dados:
        return None

    versao = dados.get("versao_contrato")
    if versao != VERSAO_CONTRATO:
        # Não tentamos adivinhar um formato antigo: avisamos. Melhor a página
        # dizer "formato desconhecido" do que desenhar campo errado calado.
        dados["_incompativel"] = (
            f"resultado.json está na versão {versao}, o leitor espera "
            f"{VERSAO_CONTRATO}."
        )
    return dados


if __name__ == "__main__":
    resultado = ler_resultado()
    if resultado is None:
        print("Nenhuma rodada publicada ainda "
              f"(esperado em {ARQUIVO_RESULTADO}).")
        raise SystemExit(0)

    print("=" * 72)
    print(f"RESULTADO PUBLICADO — {resultado['indicador'].upper()}")
    print("=" * 72)
    print(f"Rodada em: {resultado['rodada_em']} ({resultado['rodada_em_utc']})")

    p = resultado["previsao"]
    print(f"\nPrevisão: {p['valor']}%  "
          f"[{p['intervalo_min']}, {p['intervalo_max']}]  via {p['modelo']}")

    par = resultado["parecer"]
    print(f"Parecer:  {'APROVADO' if par['aprovado'] else 'NÃO APROVADO'} "
          f"({par['decisao']}, origem: {par['origem']})")
    for motivo in par["motivos"]:
        print(f"  - {motivo}")

    if resultado.get("alerta"):
        print(f"\n!! {resultado['alerta']['texto']}")

    serie = resultado["serie"]
    print(f"\nSérie: {len(serie)} pontos, de {serie[0]['data']} a "
          f"{serie[-1]['data']}")

    print("\n" + "-" * 72)
    print(resultado["relatorio"])
