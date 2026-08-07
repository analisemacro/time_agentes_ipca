# registro.py
#
# Caderno de acerto: o histórico de previsões contra o que de fato aconteceu.
#
# Papel: dar sentido a prever ANTES. Uma previsão feita na véspera só vale alguma
# coisa se ficar registrada como era na época e puder ser confrontada com o valor
# real depois. Sem este arquivo, o sistema faz previsões e ninguém nunca fica
# sabendo se elas prestam.
#
# Uma LINHA por mês de referência, em duas etapas:
#
#   1. Quando o time prevê   -> grava previsto_em, referencia, valor_previsto,
#                               intervalo (mínimo/máximo/nível) e modelo.
#   2. Quando o real sai     -> COMPLETA a mesma linha com valor_real, erro,
#                               conferido_em e observado_em.
#
# A linha é COMPLETADA, nunca reescrita. O que o sistema achava na época fica
# intacto: o valor previsto, o intervalo e o modelo continuam exatamente como
# foram gravados. Só entram campos novos, nas colunas que estavam vazias. É o que
# permite olhar para trás e ver o que o sistema pensava, e não uma versão
# corrigida depois do fato.
#
# POR QUE DUAS DATAS NO FIM. `observado_em` é o dia em que VIMOS o valor real na
# fonte; `conferido_em` é o dia em que a comparação foi registrada. Na prática
# costumam coincidir, mas o IBGE revisa número — se o valor de julho mudar em
# outubro, dá para saber se o erro guardado foi calculado contra o que estava
# publicado na época ou contra o que vale hoje. Sem isso, uma revisão silenciosa
# reescreve a história do acerto do sistema e ninguém percebe.
#
# FORMATO: CSV em data/previsoes.csv. Escolhido para o dashboard virar tabela com
# uma linha (`pd.read_csv`), e para abrir no Excel ou num editor de texto sem
# ferramenta nenhuma. Uma linha por mês de referência, ordenado por referência.
#
# Código puro, sem LLM. Guardar e comparar número é contabilidade, não julgamento.

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

from persistencia import escrever_atomico

PASTA_DADOS = Path(__file__).parent / "data"
ARQUIVO = PASTA_DADOS / "previsoes.csv"

# A ordem das colunas é a ordem de leitura: primeiro o que se sabia ao prever,
# depois o que se soube ao conferir. Quem abrir o arquivo lê a história na ordem
# em que ela aconteceu, da esquerda para a direita.
COLUNAS = [
    "referencia",        # mês previsto, "AAAA-MM" — a chave da linha
    "previsto_em",       # quando a previsão foi feita
    "valor_previsto",    # o número
    "intervalo_min",     # piso do intervalo
    "intervalo_max",     # teto do intervalo
    "intervalo_nivel",   # 0.95 = 95%
    "modelo",            # quem gerou, ex.: "ARIMA(1,0,1)"
    "origem",            # "agendada" (job automático) ou "manual" (botão)
    # --- daqui para a direita só é preenchido quando o real sai ---
    "valor_real",        # o que de fato saiu
    "erro",              # previsto - real (positivo = superestimou)
    "observado_em",      # quando VIMOS esse valor na fonte
    "conferido_em",      # quando a comparação foi registrada
]


def chave_referencia(ano: int, mes: int) -> str:
    """(2026, 7) -> "2026-07". A chave que identifica a linha."""
    return f"{ano:04d}-{mes:02d}"


def _ler() -> dict[str, dict]:
    """Lê o CSV como {referencia: linha}. Vazio se o arquivo não existe."""
    if not ARQUIVO.exists():
        return {}
    try:
        with ARQUIVO.open(encoding="utf-8", newline="") as f:
            return {linha["referencia"]: linha for linha in csv.DictReader(f)}
    except (OSError, csv.Error):
        # Mesmo critério do memoria.py: arquivo ilegível é tratado como vazio,
        # nunca apagado nem "consertado" com chute.
        return {}


def _escrever(linhas: dict[str, dict]) -> None:
    """Persiste ordenado por mês de referência — arquivo estável de auditar.

    Escrita ATÔMICA (via persistencia.escrever_atomico): este arquivo é o acervo
    do sistema e roda numa máquina que pode morrer no meio. Um CSV truncado aqui
    apagaria meses de histórico de acerto sem ninguém perceber.
    """
    buffer = io.StringIO()
    escritor = csv.DictWriter(buffer, fieldnames=COLUNAS)
    escritor.writeheader()
    for chave in sorted(linhas):
        # restval="" preenche coluna ausente com vazio em vez de quebrar.
        escritor.writerow({c: linhas[chave].get(c, "") for c in COLUNAS})
    escrever_atomico(ARQUIVO, buffer.getvalue())


def registrar_previsao(ano: int, mes: int, previsao: dict,
                       quando: str | None = None,
                       origem: str = "agendada") -> dict:
    """Abre (ou refaz) a linha de um mês de referência com o que foi previsto.

    Rodar duas vezes no mesmo dia NÃO duplica: a chave é o mês de referência, e
    a segunda gravação substitui a primeira em vez de criar uma segunda linha.

    Se a linha JÁ FOI CONFERIDA (tem valor real), ela não é mexida: reprever um
    mês cujo número já saiu não é previsão, é palpite com o gabarito na mão, e
    deixar isso entrar apagaria um acerto ou erro real do histórico.

    Args:
        ano, mes: o mês de REFERÊNCIA previsto (o mês a que o dado se refere,
            não o mês em que a previsão foi feita — o IPCA divulgado em agosto
            é o de julho).
        previsao: o dict do previsor, no formato que `no_registrar_previsao`
            escreve: {"status": "ok", "valor": 0.42, "intervalo": {"minimo":
            ..., "maximo": ..., "nivel": ...}, "modelo": "ARIMA(1,0,1)"}.
        quando: data ISO da previsão. Padrão: hoje.
        origem: "agendada" (o job automático) ou "manual" (o botão do
            dashboard). Fica no CSV para o histórico distinguir o que o sistema
            fez sozinho do que foi disparado na mão — sem isso, uma rodada
            manual feita depois da divulgação se misturaria às automáticas e
            inflaria artificialmente o acerto medido.

    Returns:
        A linha gravada. Se a previsão não tem número (o previsor recusou), nada
        é gravado e devolve-se um dict com "status": "ignorada" — linha sem
        número previsto não serve para medir acerto.
    """
    if previsao.get("status") != "ok" or previsao.get("valor") is None:
        return {"status": "ignorada",
                "motivo": (f"previsão sem número "
                           f"({previsao.get('status', 'sem status')}) — não há "
                           f"o que registrar para medir acerto.")}

    linhas = _ler()
    chave = chave_referencia(ano, mes)

    ja_existente = linhas.get(chave)
    if ja_existente and ja_existente.get("valor_real"):
        return {"status": "ignorada",
                "motivo": (f"a linha de {chave} já foi conferida contra o valor "
                           f"real — não se reescreve previsão de mês fechado."),
                "linha": ja_existente}

    intervalo = previsao.get("intervalo") or {}
    linha = {
        "referencia": chave,
        "previsto_em": quando or date.today().isoformat(),
        "valor_previsto": previsao["valor"],
        "intervalo_min": intervalo.get("minimo", ""),
        "intervalo_max": intervalo.get("maximo", ""),
        "intervalo_nivel": intervalo.get("nivel", ""),
        "modelo": previsao.get("modelo", ""),
        "origem": origem,
        # As colunas da conferência nascem vazias, de propósito: vazio quer dizer
        # "ainda esperando o dado sair", e é assim que `pendentes()` as encontra.
        "valor_real": "",
        "erro": "",
        "observado_em": "",
        "conferido_em": "",
    }

    linhas[chave] = linha
    _escrever(linhas)
    return linha


def registrar_realizado(ano: int, mes: int, valor_real: float,
                        observado_em: str | None = None,
                        conferido_em: str | None = None) -> dict | None:
    """COMPLETA a linha do mês com o valor que de fato saiu.

    Não toca em nada do que já estava lá: valor previsto, intervalo, modelo e
    data da previsão continuam como foram gravados. Só preenche as quatro
    colunas da direita, que estavam vazias.

    Args:
        ano, mes: o mês de referência do valor que saiu.
        valor_real: o número publicado pela fonte.
        observado_em: quando esse valor foi VISTO na fonte. Padrão: hoje.
            Guardado à parte porque o IBGE revisa: serve para saber se o erro
            foi calculado contra o que estava publicado na época.
        conferido_em: quando a comparação foi registrada. Padrão: hoje.

    Returns:
        A linha completa, ou None se não havia previsão para esse mês — não
        inventamos linha para um mês que o sistema nunca previu.
    """
    linhas = _ler()
    chave = chave_referencia(ano, mes)
    linha = linhas.get(chave)
    if linha is None:
        return None

    hoje = date.today().isoformat()
    real = float(valor_real)

    linha["valor_real"] = real
    linha["observado_em"] = observado_em or hoje
    linha["conferido_em"] = conferido_em or observado_em or hoje

    # erro = previsto - real. Positivo: o sistema previu inflação maior do que a
    # que veio. Arredondado em 4 casas para não carregar lixo de ponto flutuante.
    try:
        linha["erro"] = round(float(linha["valor_previsto"]) - real, 4)
    except (TypeError, ValueError):
        linha["erro"] = ""

    linhas[chave] = linha
    _escrever(linhas)
    return linha


def ler_linha(ano: int, mes: int) -> dict | None:
    """A linha de um mês de referência, ou None."""
    return _ler().get(chave_referencia(ano, mes))


def tem_previsao(ano: int, mes: int) -> bool:
    """Já existe previsão registrada para este mês? (o que a sentinela pergunta)"""
    return chave_referencia(ano, mes) in _ler()


def pendentes() -> list[dict]:
    """Previsões ainda esperando o valor real — coluna valor_real vazia."""
    return [l for l in historico() if not l.get("valor_real")]


def conferidas() -> list[dict]:
    """Previsões que já têm valor real ao lado."""
    return [l for l in historico() if l.get("valor_real")]


def historico() -> list[dict]:
    """Todas as linhas, em ordem de mês de referência."""
    linhas = _ler()
    return [linhas[chave] for chave in sorted(linhas)]


def desempenho() -> dict:
    """Resumo do acerto sobre as linhas já conferidas.

    Devolve erro médio (viés: positivo = o sistema superestima a inflação),
    erro absoluto médio (tamanho típico do erro, sem compensar sinal) e quantas
    vezes o valor real caiu dentro do intervalo previsto — a cobertura, que é o
    teste honesto de um intervalo de confiança.
    """
    fechadas = conferidas()
    if not fechadas:
        return {"n": 0, "motivo": "nenhuma previsão conferida ainda."}

    erros: list[float] = []
    dentro = 0
    com_intervalo = 0

    for linha in fechadas:
        try:
            erros.append(float(linha["erro"]))
        except (TypeError, ValueError):
            continue
        try:
            minimo = float(linha["intervalo_min"])
            maximo = float(linha["intervalo_max"])
            real = float(linha["valor_real"])
        except (TypeError, ValueError):
            continue
        com_intervalo += 1
        if minimo <= real <= maximo:
            dentro += 1

    if not erros:
        return {"n": len(fechadas), "motivo": "linhas conferidas sem erro válido."}

    return {
        "n": len(erros),
        "erro_medio": round(sum(erros) / len(erros), 4),
        "erro_absoluto_medio": round(sum(abs(e) for e in erros) / len(erros), 4),
        "dentro_do_intervalo": dentro,
        "com_intervalo": com_intervalo,
        "cobertura": (round(dentro / com_intervalo, 4) if com_intervalo else None),
    }


def carregar_tabela():
    """Devolve o histórico como DataFrame do pandas, pronto para o dashboard.

    Conveniência para quem já tem pandas (o dashboard tem). Quem não tem lê o
    CSV direto: é o mesmo arquivo, sem mágica no meio.
    """
    import pandas as pd

    if not ARQUIVO.exists():
        return pd.DataFrame(columns=COLUNAS)
    return pd.read_csv(ARQUIVO)


if __name__ == "__main__":
    print(f"Arquivo: {ARQUIVO}")
    linhas = historico()
    if not linhas:
        print("(vazio — nenhuma previsão registrada ainda)")
    else:
        for linha in linhas:
            estado = ("conferida" if linha.get("valor_real") else "esperando o dado")
            print(f"  {linha['referencia']}  previsto {linha['valor_previsto']}"
                  f"  [{estado}]")
        print()
        print("Desempenho:", desempenho())
