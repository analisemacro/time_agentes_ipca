# persistencia.py
#
# O que sobrevive quando a máquina é destruída — e como gravar sem corromper.
#
# Papel: este módulo é a resposta a uma pergunta que só aparece quando o sistema
# sai da sua máquina e vai para o GitHub Actions: o runner é criado do zero a
# cada execução e APAGADO no fim. Nada que o programa escreve em disco sobrevive
# por conta própria. O que precisa durar tem que ser commitado de volta ao
# repositório; o resto é lixo que morre junto com a máquina, e tudo bem.
#
# ============================================================================
# O QUE SOBREVIVE (acervo — commitar de volta, SEMPRE)
# ============================================================================
#
#   data/previsoes.csv    O histórico de previsões contra o realizado. É o
#                         ACERVO do sistema: a única prova de que ele acerta (ou
#                         não). Não dá para reconstruir — uma previsão feita em
#                         julho não pode ser refeita em outubro, porque em
#                         outubro já se sabe a resposta. Perder este arquivo
#                         apaga a memória de desempenho para sempre.
#
#   data/estado.json      O que a sentinela viu na rodada anterior: última data
#                         observada por indicador e quando foi a última rodada.
#                         Sem ele, a sentinela acorda amnésica todo dia e não
#                         consegue responder "a série avançou?" — porque não sabe
#                         de onde ela veio.
#
#   data/resultado.json   O contrato com o dashboard: o resultado da última
#                         rodada (série, previsão, parecer, relatório). É o que
#                         a página web lê. Sem ele, o dashboard não tem o que
#                         mostrar até a próxima rodada rodar. Ver publicacao.py.
#
#   data/<indicador>.json A série coletada (ex.: data/ipca.json). Tecnicamente
#                         reconstruível baixando tudo de novo, mas guardá-la
#                         evita rebaixar 20 anos de série a cada rodada e é o que
#                         o time lê para prever.
#
# ============================================================================
# O QUE É DESCARTÁVEL (não commitar — morre com a máquina, sem prejuízo)
# ============================================================================
#
#   data/checkpoints.sqlite   Estado do grafo LangGraph por thread_id. Serve para
#                             retomar UMA execução interrompida no meio. Entre
#                             execuções não vale nada: a rodada seguinte começa
#                             do zero de propósito. São 5,8 MB que cresceriam a
#                             cada rodada e inchariam o repositório para sempre.
#
#   logs/                     Auditoria das chamadas de LLM. Útil para depurar na
#                             hora; no Actions, sai como artefato da execução, não
#                             como commit.
#
#   __pycache__/, *.pyc       Cache do Python.
#
#   *.tmp                     Restos de gravação atômica (ver abaixo).
#
# ============================================================================
# GRAVAÇÃO SEGURA
# ============================================================================
#
# O problema, medido antes de escrever este módulo: abrir um arquivo em modo "w"
# TRUNCA o arquivo na hora. Se o processo morrer entre o truncamento e o fim da
# escrita — timeout do Actions, runner morto, disco cheio — o que sobra é um
# arquivo pela metade, e o conteúdo anterior sumiu. Testei: um previsoes.csv com
# uma linha boa virou a string "cabecalho" e o resto se perdeu.
#
# Num sistema que roda sozinho, isso é pior do que parece: a rodada seguinte lê
# lixo, não encontra as previsões, e o caderno de acerto de meses volta ao zero
# sem ninguém perceber.
#
# A solução é gravar em três tempos:
#
#   1. escreve o conteúdo NOVO num arquivo temporário ao lado (arquivo.tmp);
#   2. força o conteúdo para o disco (flush + fsync) — sem isso o dado pode
#      estar só no buffer do sistema operacional quando a máquina cair;
#   3. RENOMEIA o temporário por cima do definitivo (os.replace), que é uma
#      operação atômica: ou o arquivo antigo continua inteiro, ou o novo está
#      inteiro. Nunca um meio-termo.
#
# Morrer no passo 1 ou 2 deixa um .tmp órfão e NÃO toca no arquivo bom. Morrer
# no passo 3 é impossível de deixar pela metade — é o que "atômico" quer dizer.

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

PASTA_DADOS = Path(__file__).parent / "data"
ARQUIVO_ESTADO = PASTA_DADOS / "estado.json"

# Os arquivos que precisam ser commitados de volta ao repositório no fim de cada
# execução no Actions. Lista usada pelo workflow e pelo relatório de diagnóstico
# — um lugar só para essa verdade, em vez de espalhada por comentários.
ARQUIVOS_QUE_SOBREVIVEM = [
    "data/previsoes.csv",
    "data/estado.json",
    "data/resultado.json",
    "data/ipca.json",
]

ARQUIVOS_DESCARTAVEIS = [
    "data/checkpoints.sqlite",
    "logs/",
    "__pycache__/",
    "*.tmp",
]


# ---------------------------------------------------------------------------
# Gravação atômica — a base de tudo que se escreve aqui.
# ---------------------------------------------------------------------------
def escrever_atomico(caminho: Path, conteudo: str) -> None:
    """Grava `conteudo` em `caminho` sem risco de deixar o arquivo pela metade.

    Escreve num .tmp ao lado, força para o disco e renomeia por cima. Se o
    processo morrer no meio, o arquivo original continua íntegro e sobra no
    máximo um .tmp órfão (inofensivo, e limpo por `limpar_temporarios()`).

    O .tmp fica na MESMA pasta de propósito: os.replace só é atômico dentro do
    mesmo sistema de arquivos, e um temp em /tmp poderia estar noutro disco.
    """
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(caminho.suffix + ".tmp")

    try:
        # newline="" para o csv não duplicar \r\n no Windows.
        with temporario.open("w", encoding="utf-8", newline="") as f:
            f.write(conteudo)
            f.flush()
            # fsync: sem isto o conteúdo pode estar só no buffer do sistema
            # operacional, e uma queda entre o write e a gravação real deixaria
            # um arquivo vazio com nome de arquivo bom.
            os.fsync(f.fileno())

        # A troca. Atômica: ou fica o antigo inteiro, ou o novo inteiro.
        os.replace(temporario, caminho)
    except BaseException:
        # Inclui KeyboardInterrupt/SystemExit (BaseException, não Exception):
        # se o processo está sendo morto, ainda assim não deixamos lixo para
        # trás — e o arquivo bom nunca chegou a ser tocado.
        temporario.unlink(missing_ok=True)
        raise


def ler_json(caminho: Path, padrao: dict | None = None) -> dict:
    """Lê um JSON. Arquivo ausente ou ilegível devolve `padrao` (não explode).

    Mesmo critério do memoria.py: arquivo corrompido é tratado como vazio, nunca
    apagado nem "consertado" com chute. Com a gravação atômica acima, chegar aqui
    com arquivo corrompido virou situação quase impossível — mas o sistema roda
    sozinho, e é melhor começar do zero do que derrubar a rodada.
    """
    if padrao is None:
        padrao = {}
    if not caminho.exists():
        return dict(padrao)
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return dict(padrao)


def escrever_json(caminho: Path, dados: dict) -> None:
    """Grava um dict como JSON legível, de forma atômica."""
    escrever_atomico(caminho,
                     json.dumps(dados, ensure_ascii=False, indent=2) + "\n")


def limpar_temporarios(pasta: Path | None = None) -> list[str]:
    """Remove .tmp órfãos de uma execução que morreu no meio da escrita.

    Chamar no começo da rodada. Não é obrigatório (um .tmp órfão não atrapalha
    ninguém), mas evita acumular lixo no repositório ao longo dos meses.
    """
    alvo = pasta or PASTA_DADOS
    if not alvo.exists():
        return []
    removidos = []
    for restante in alvo.glob("*.tmp"):
        restante.unlink(missing_ok=True)
        removidos.append(restante.name)
    return removidos


# ---------------------------------------------------------------------------
# O arquivo de estado — a memória da sentinela entre execuções.
# ---------------------------------------------------------------------------
def ler_estado() -> dict:
    """Lê data/estado.json. Estrutura vazia se for a primeira execução."""
    return ler_json(ARQUIVO_ESTADO, padrao={"indicadores": {}, "rodadas": {}})


def ultima_data_observada(indicador: str) -> str | None:
    """A última data que a sentinela viu na fonte para este indicador.

    É contra este valor que a próxima rodada compara para responder "a série
    avançou?". None na primeira execução.
    """
    estado = ler_estado()
    registro = (estado.get("indicadores") or {}).get(indicador) or {}
    return registro.get("ultima_data_observada")


def anotar_observacao(indicador: str, data_observada: str,
                      valor: float | None = None,
                      quando: str | None = None) -> dict:
    """Anota que vimos a fonte publicando `data_observada` para o indicador.

    Guarda também `visto_pela_primeira_vez_em`: o dia em que ESTA data apareceu
    pela primeira vez. Enquanto a série não anda, esse campo não muda — dá para
    ver há quanto tempo a fonte está parada, que é a pergunta que aparece quando
    o IBGE atrasa uma divulgação.
    """
    estado = ler_estado()
    indicadores = estado.setdefault("indicadores", {})
    anterior = indicadores.get(indicador) or {}
    agora = quando or date.today().isoformat()

    mudou = anterior.get("ultima_data_observada") != data_observada

    indicadores[indicador] = {
        "ultima_data_observada": data_observada,
        "ultimo_valor_observado": valor,
        "visto_pela_primeira_vez_em": (
            agora if mudou else anterior.get("visto_pela_primeira_vez_em", agora)
        ),
        "conferido_em": agora,
    }

    escrever_json(ARQUIVO_ESTADO, estado)
    return indicadores[indicador]


def anotar_rodada(acao: str, quando: str | None = None,
                  detalhe: str | None = None) -> dict:
    """Registra que a sentinela rodou, e com que desfecho.

    Guarda a última rodada de cada tipo de ação, não um log crescente: o arquivo
    de estado precisa ter tamanho constante, senão vira um log com nome de
    estado e cresce a cada dia no repositório. Quem quiser histórico completo
    olha os logs da execução no Actions.
    """
    estado = ler_estado()
    rodadas = estado.setdefault("rodadas", {})
    agora = quando or date.today().isoformat()

    rodadas["ultima"] = {"em": agora, "acao": acao, "detalhe": detalhe}
    rodadas[f"ultima_{acao}"] = {"em": agora, "detalhe": detalhe}
    rodadas["carimbo_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    escrever_json(ARQUIVO_ESTADO, estado)
    return rodadas["ultima"]


def diagnostico() -> dict:
    """O que existe em disco agora, e o que disso sobreviveria à máquina morrer.

    Serve para conferir, no fim de uma execução no Actions, se o que precisa ser
    commitado está lá — antes de descobrir na rodada seguinte que não estava.
    """
    raiz = Path(__file__).parent

    def _info(rel: str) -> dict:
        caminho = raiz / rel
        return {
            "existe": caminho.exists(),
            "bytes": caminho.stat().st_size if caminho.is_file() else None,
        }

    return {
        "sobrevivem": {rel: _info(rel) for rel in ARQUIVOS_QUE_SOBREVIVEM},
        "descartaveis": {
            rel: _info(rel) for rel in ARQUIVOS_DESCARTAVEIS
            if not rel.endswith(("/", "*.tmp"))
        },
        "temporarios_orfaos": [p.name for p in PASTA_DADOS.glob("*.tmp")],
    }


if __name__ == "__main__":
    print("=" * 72)
    print("PERSISTÊNCIA — o que sobrevive à máquina ser destruída")
    print("=" * 72)

    diag = diagnostico()

    print("\nSOBREVIVEM (commitar de volta no fim da execução):")
    for rel, info in diag["sobrevivem"].items():
        marca = "ok " if info["existe"] else "AUSENTE"
        tam = f"{info['bytes']} bytes" if info["bytes"] is not None else "-"
        print(f"  [{marca}] {rel:24} {tam}")

    print("\nDESCARTÁVEIS (morrem com a máquina, sem prejuízo):")
    for rel, info in diag["descartaveis"].items():
        tam = f"{info['bytes']} bytes" if info["bytes"] is not None else "-"
        print(f"  [   ] {rel:24} {tam}")
    for rel in ARQUIVOS_DESCARTAVEIS:
        if rel.endswith(("/", "*.tmp")):
            print(f"  [   ] {rel}")

    orfaos = diag["temporarios_orfaos"]
    print(f"\nTemporários órfãos: {orfaos if orfaos else 'nenhum'}")

    print("\n" + "-" * 72)
    print("ESTADO ATUAL (data/estado.json)")
    print("-" * 72)
    print(json.dumps(ler_estado(), ensure_ascii=False, indent=2))
