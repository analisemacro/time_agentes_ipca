"""
Módulo de coleta de dados — Sistema multi-agente de previsão do IPCA.

Este módulo é CÓDIGO PURO (sem LLM). Baixa a variável-alvo (IPCA) e um conjunto
de variáveis exógenas de fontes oficiais para alimentar o ensemble de modelos.

Regra de ouro do projeto: NÃO inventar números. Todo dado vem de uma API oficial
(BCB/SGS, FRED). Todo código de série abaixo foi verificado consultando a API real
antes de ser inserido aqui — não confie em códigos "de memória"; se precisar
adicionar uma série, confirme o código na fonte primeiro.

Fontes:
  - BCB/SGS  : https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados
  - FRED     : https://fred.stlouisfed.org/graph/fredgraph.csv?id={codigo}

Saída: um DataFrame indexado por data (mensal) com o IPCA e as exógenas, mais um
relatório de validação. Nenhum valor é preenchido/estimado aqui — imputação e
transformação ficam no módulo de tratamento.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from urllib.error import HTTPError
from urllib.request import urlopen

import pandas as pd


# ---------------------------------------------------------------------------
# Catálogo de séries — a fonte da verdade sobre o que é coletado.
#
# Cada entrada: (código, nome_interno, fonte, frequência, papel, descrição).
#   - papel "alvo"  : variável a prever (IPCA).
#   - papel "exog"  : regressor candidato para o ensemble.
#
# Os códigos SGS/FRED abaixo foram TODOS verificados contra a API real
# (retornaram dados recentes e plausíveis) em julho/2026.
# ---------------------------------------------------------------------------
SERIES = [
    # ----- Variável-alvo -----------------------------------------------------
    ("433",  "ipca",              "SGS",  "M", "alvo", "IPCA – variação mensal (%)"),

    # ----- Núcleos e prévias de inflação (poder preditivo direto) -----------
    ("7478", "ipca15",            "SGS",  "M", "exog", "IPCA-15 – prévia mensal (%)"),
    ("4466", "ipca_ma",           "SGS",  "M", "exog", "IPCA núcleo médias aparadas c/ suavização (%)"),
    ("27863","ipca_ex0",          "SGS",  "M", "exog", "IPCA núcleo por exclusão EX0 (%)"),
    ("189",  "igpm",              "SGS",  "M", "exog", "IGP-M – variação mensal (%)"),
    ("4449", "ipca_livres",       "SGS",  "M", "exog", "IPCA preços livres – variação mensal (%)"),

    # ----- Atividade e mercado de trabalho (pressão de demanda) -------------
    ("24364","ibc_br",            "SGS",  "M", "exog", "IBC-Br – índice de atividade econômica (dessaz.)"),
    ("24369","desemprego",        "SGS",  "M", "exog", "Taxa de desocupação PNAD Contínua (%)"),
    ("22707","saldo_caged",       "SGS",  "M", "exog", "Saldo de empregos formais (Novo CAGED)"),

    # ----- Política monetária e câmbio (canal de transmissão) ---------------
    # Selic efetiva (1178) cobre 2004→hoje com 0% de NA. A "meta Selic" (21082)
    # só existe no SGS a partir de 03/2011 e seria redundante — fora do catálogo
    # de propósito, para não injetar 7 anos de NA no ensemble.
    ("1178", "selic",             "SGS",  "D", "exog", "Taxa Selic anualizada (%)"),
    ("1",    "cambio",            "SGS",  "D", "exog", "Câmbio BRL/USD – venda"),

    # ----- Choques de oferta externos (via FRED) ----------------------------
    ("DCOILBRENTEU", "petroleo",  "FRED", "D", "exog", "Petróleo Brent – US$/barril"),
]


# ---------------------------------------------------------------------------
# Funções de coleta — reaproveitadas do pipeline irmão (previsao_macro), já
# validadas em produção: retry com backoff, fatiamento de janela p/ séries
# diárias (limite da API do SGS).
# ---------------------------------------------------------------------------
def ler_csv(*args, max_tentativas: int = 5, intervalo: int = 2, **kwargs):
    """Lê um CSV com retry e backoff simples. Retorna None se falhar tudo."""
    for tentativa in range(1, max_tentativas + 1):
        try:
            return pd.read_csv(*args, **kwargs)
        except Exception as e:  # rede/HTTP/parse — todos justificam nova tentativa
            print(f"  Tentativa {tentativa}/{max_tentativas} falhou: {e}")
            time.sleep(intervalo)
    print(f"  Falha após {max_tentativas} tentativas.")
    return None


def _ler_csv_sgs(url: str, max_tentativas: int = 5, intervalo: int = 2):
    """Lê uma janela do SGS distinguindo 404 (janela vazia) de falha real.

    Retorna (DataFrame|None, vazia:bool). Um HTTP 404 do SGS significa
    "sem valores neste intervalo" — a série começa depois desta janela; isso
    é legítimo, não um erro, e NÃO deve ser confundido com queda de rede.
    """
    from io import StringIO

    for tentativa in range(1, max_tentativas + 1):
        try:
            with urlopen(url, timeout=30) as resp:
                texto = resp.read().decode("utf-8", errors="replace")
            return pd.read_csv(StringIO(texto), sep=";", decimal=","), False
        except HTTPError as e:
            if e.code == 404:
                return None, True  # janela vazia legítima — não retentar
            print(f"  Tentativa {tentativa}/{max_tentativas} (HTTP {e.code})")
            time.sleep(intervalo)
        except Exception as e:
            print(f"  Tentativa {tentativa}/{max_tentativas} falhou: {e}")
            time.sleep(intervalo)
    return None, False  # esgotou tentativas: falha real


def split_date_range(inicio: str, fim: str, intervalo_anos: int = 5):
    """Fatia [inicio, fim] em janelas de N anos (dd/mm/aaaa).

    A API do SGS limita o volume por requisição em séries diárias; coletamos
    em blocos e concatenamos.
    """
    d_ini = datetime.strptime(inicio, "%d/%m/%Y")
    d_fim = datetime.strptime(fim, "%d/%m/%Y")
    janelas, atual = [], d_ini
    while atual < d_fim:
        try:
            prox = atual.replace(year=atual.year + intervalo_anos)
        except ValueError:  # 29/02 em ano não-bissexto
            prox = atual + timedelta(days=365 * intervalo_anos)
        prox = min(prox, d_fim)
        janelas.append((atual.strftime("%d/%m/%Y"), prox.strftime("%d/%m/%Y")))
        atual = prox
    return janelas


def coleta_bcb_sgs(codigo: str, nome: str, freq: str,
                   data_inicio: str = "01/01/2000",
                   data_fim: str | None = None) -> pd.DataFrame | None:
    """Coleta uma série da API do BCB/SGS. Retorna DataFrame indexado por data."""
    if data_fim is None:
        data_fim = pd.to_datetime("today").strftime("%d/%m/%Y")

    janelas = split_date_range(data_inicio, data_fim) if freq == "D" else [(data_inicio, data_fim)]

    print(f"Coletando SGS {codigo} ({nome})")
    partes, janelas_ok, janelas_vazias = [], 0, 0
    for ini, fim in janelas:
        url = (f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"
               f"?formato=csv&dataInicial={ini}&dataFinal={fim}")
        parte, vazia = _ler_csv_sgs(url)
        if vazia:
            # 404 do SGS = "sem dados nesta janela" (a série começa depois).
            # É legítimo — a série só não existe nesse período. Não é falha.
            janelas_vazias += 1
            continue
        if parte is not None:
            partes.append(parte)
            janelas_ok += 1

    # Se NENHUMA janela retornou dados E também nenhuma foi "vazia legítima",
    # foi falha de rede de verdade — devolve None p/ o relatório marcar.
    if not partes and janelas_vazias == 0:
        return None
    if not partes:
        # Todas as janelas eram vazias: a série realmente não tem dado no período.
        return None
    return (
        pd.concat(partes)
        .rename(columns={"valor": nome})
        .assign(data=lambda d: pd.to_datetime(d["data"], format="%d/%m/%Y"))
        .set_index("data")
        .filter([nome])
    )


def coleta_fred(codigo: str, nome: str) -> pd.DataFrame | None:
    """Coleta uma série do FRED (St. Louis Fed). Retorna DataFrame por data."""
    print(f"Coletando FRED {codigo} ({nome})")
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={codigo}"
    resposta = ler_csv(url)
    if resposta is None:
        return None
    return (
        resposta
        .rename(columns={"DATE": "data", "observation_date": "data", codigo: nome})
        .assign(data=lambda d: pd.to_datetime(d["data"]))
        # FRED marca dados ausentes como ".", que vira NaN — não inventamos valor.
        .assign(**{nome: lambda d: pd.to_numeric(d[nome], errors="coerce")})
        .set_index("data")
        .filter([nome])
    )


# ---------------------------------------------------------------------------
# Orquestração da coleta + validação (o antídoto contra alucinação)
# ---------------------------------------------------------------------------
def _coleta_uma(entrada) -> pd.DataFrame | None:
    codigo, nome, fonte, freq, _papel, _desc = entrada
    if fonte == "SGS":
        return coleta_bcb_sgs(codigo, nome, freq)
    if fonte == "FRED":
        return coleta_fred(codigo, nome)
    raise ValueError(f"Fonte desconhecida: {fonte}")


def _mensaliza(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Séries diárias viram média mensal; mensais são reindexadas para início do mês.
    É a única agregação feita aqui (média de dados REAIS, não estimativa)."""
    if freq == "D":
        return df.resample("MS").mean()
    return df.resample("MS").last()


def coletar(data_inicio: str = "01/01/2004", verbose: bool = True):
    """Coleta todas as séries do catálogo, mensaliza e junta num único DataFrame.

    Retorna (df, relatorio) onde `relatorio` é um DataFrame com o resultado da
    validação por série. NÃO faz imputação nem transformação — dado cru e honesto.
    """
    coletados, faltantes = {}, []

    for entrada in SERIES:
        codigo, nome, fonte, freq, papel, desc = entrada
        df = _coleta_uma(entrada)
        if df is None or df.empty:
            faltantes.append(nome)
            print(f"  [FALHA] {nome} ({fonte} {codigo}) não retornou dados.")
            continue
        df = df[df.index >= pd.to_datetime(data_inicio, format="%d/%m/%Y")]
        coletados[nome] = _mensaliza(df, freq)

    if "ipca" not in coletados:
        # Sem a variável-alvo não há previsão possível — falha explícita.
        raise RuntimeError(
            "Coleta do IPCA (alvo) falhou. Abortando: previsão exige a série-alvo."
        )

    df = pd.concat(coletados.values(), axis=1).sort_index().asfreq("MS")

    relatorio = validar(df, faltantes)
    if verbose:
        _imprime_relatorio(relatorio, faltantes)
    return df, relatorio


# ---------------------------------------------------------------------------
# Validação anti-alucinação — verifica que o dado é REAL e recente, sem
# nunca preencher/estimar. Se algo está errado, o relatório mostra; não maquia.
# ---------------------------------------------------------------------------
def validar(df: pd.DataFrame, faltantes: list[str]) -> pd.DataFrame:
    """Gera um relatório de sanidade por série. Não altera os dados."""
    catalogo = {nome: (fonte, codigo, papel, desc)
                for codigo, nome, fonte, freq, papel, desc in SERIES}
    hoje = pd.to_datetime("today").normalize()
    linhas = []
    for col in df.columns:
        s = df[col].dropna()
        fonte, codigo, papel, desc = catalogo.get(col, ("?", "?", "?", ""))
        ultimo = s.index.max() if not s.empty else pd.NaT
        atraso_meses = ((hoje.year - ultimo.year) * 12 + hoje.month - ultimo.month) if pd.notna(ultimo) else None
        linhas.append({
            "serie": col,
            "papel": papel,
            "fonte": fonte,
            "codigo": codigo,
            "n_obs": int(s.shape[0]),
            "inicio": s.index.min() if not s.empty else pd.NaT,
            "fim": ultimo,
            "atraso_meses": atraso_meses,
            "pct_faltante": round(float(df[col].isna().mean() * 100), 1),
            "todos_iguais": bool(s.nunique() <= 1) if not s.empty else True,
        })
    rel = pd.DataFrame(linhas).set_index("serie")

    # Sinalizadores de suspeita — cada um é um jeito conhecido de "parecer certo
    # mas estar errado". Não corrigimos aqui; apenas expomos.
    rel["ALERTA"] = ""
    rel.loc[rel["n_obs"] < 24, "ALERTA"] += "poucas_obs;"
    rel.loc[rel["atraso_meses"].fillna(99) > 3, "ALERTA"] += "desatualizada;"
    rel.loc[rel["pct_faltante"] > 20, "ALERTA"] += "muitos_NA;"
    rel.loc[rel["todos_iguais"], "ALERTA"] += "constante;"
    return rel


def _imprime_relatorio(rel: pd.DataFrame, faltantes: list[str]) -> None:
    print("\n" + "=" * 72)
    print("RELATÓRIO DE VALIDAÇÃO DA COLETA")
    print("=" * 72)
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(rel[["papel", "fonte", "codigo", "n_obs", "inicio", "fim",
                   "atraso_meses", "pct_faltante", "ALERTA"]].to_string())
    suspeitas = rel[rel["ALERTA"] != ""]
    print("-" * 72)
    if faltantes:
        print(f"[!] Séries que NÃO retornaram dados: {', '.join(faltantes)}")
    if not suspeitas.empty:
        print(f"[!] {len(suspeitas)} série(s) com alerta — revise antes de modelar:")
        for nome, row in suspeitas.iterrows():
            print(f"    - {nome}: {row['ALERTA']}")
    if faltantes or not suspeitas.empty:
        print("[i] Nenhum valor foi inventado ou preenchido. Trate na etapa de tratamento.")
    else:
        print("[OK] Todas as séries passaram nas checagens de sanidade.")
    print("=" * 72 + "\n")


if __name__ == "__main__":
    df, relatorio = coletar()
    # Persiste o dado cru; o tratamento consome isto.
    import os
    os.makedirs("dados", exist_ok=True)
    df.to_parquet("dados/df_coleta_bruta.parquet")
    relatorio.to_csv("dados/relatorio_coleta.csv")
    print(f"[OK] {df.shape[0]} meses × {df.shape[1]} séries salvos em dados/df_coleta_bruta.parquet")
