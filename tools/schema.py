# tools/schema.py
#
# Validação em CAMADAS dos pontos coletados. Filosofia: preferir FALHAR a
# entregar um número errado. Cada camada pega um tipo diferente de erro:
#
#   1. Forma (Pydantic)   — recusa data impossível e texto onde devia vir número.
#   2. Faixa plausível    — valor fora da faixa do indicador é quase sempre erro
#                           de escala ou de coleta (ex.: câmbio "50" em vez de "5").
#   3. Regra de domínio    — a série veio completa? faltam meses?
#
# A função valida_pontos devolve (validados, avisos): o que passou e o que não
# passou, cada aviso dizendo o PORQUÊ.

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, field_validator


# ---------------------------------------------------------------------------
# CAMADA 1 — Forma. O Pydantic garante que cada ponto tem uma data real e um
# valor numérico. Data impossível ("2026-13-40") ou texto no lugar do número
# são recusados aqui, antes de qualquer outra checagem.
# ---------------------------------------------------------------------------
class PontoSerie(BaseModel):
    data: date
    valor: float

    @field_validator("data", mode="before")
    @classmethod
    def _aceita_formatos_de_data(cls, v):
        """Aceita 'AAAA-MM-DD' (SGS) e 'AAAAMM' (SIDRA), além de date/datetime.

        As duas tools do projeto emitem a data em formatos diferentes; unificamos
        aqui para que o resto do pipeline veja sempre um date.
        """
        if isinstance(v, str):
            texto = v.strip()
            if texto.isdigit() and len(texto) == 6:  # AAAAMM do SIDRA
                return date(int(texto[:4]), int(texto[4:6]), 1)
        return v  # date/datetime ou 'AAAA-MM-DD' — o Pydantic resolve


# ---------------------------------------------------------------------------
# CAMADA 2 — Faixa plausível por indicador. Limites (min, max) conservadores:
# um valor fora daqui é quase sempre erro de escala ou de coleta, não um dado
# real. Ampliar/ajustar aqui é o lugar certo ao adicionar um indicador.
#   ipca  : variação mensal em % — deflação forte a hiperinflação improvável.
#   selic : taxa % a.a. — de zero a um teto folgado.
#   cambio: BRL/USD — nunca perto de 0,5 nem acima de 15 no horizonte do projeto.
# ---------------------------------------------------------------------------
FAIXAS: dict[str, tuple[float, float]] = {
    "ipca":   (-3.0, 10.0),
    "selic":  (0.0, 50.0),
    "cambio": (0.5, 15.0),
}


def _fora_da_faixa(indicador: str, valor: float) -> str | None:
    """Devolve o motivo se o valor está fora da faixa do indicador; senão None.

    Indicador sem faixa cadastrada NÃO é bloqueado (não temos como julgar), mas
    isso fica registrado no aviso para quem auditar."""
    faixa = FAIXAS.get(indicador)
    if faixa is None:
        return None
    minimo, maximo = faixa
    if not (minimo <= valor <= maximo):
        return (f"valor {valor} fora da faixa plausível de '{indicador}' "
                f"[{minimo}, {maximo}] — provável erro de escala ou coleta")
    return None


# ---------------------------------------------------------------------------
# CAMADA 3 — Regra de domínio. Uma série pode passar na forma e na faixa e ainda
# estar INCOMPLETA (buracos no meio, ou não chegou até o mês esperado). Aqui
# comparamos os meses presentes com os esperados.
# ---------------------------------------------------------------------------
def faltam_meses(serie: list[PontoSerie], esperados: list[date]) -> list[date]:
    """Retorna os meses esperados que NÃO aparecem na série (lista vazia = ok).

    Compara por (ano, mês), ignorando o dia — séries mensais são ancoradas no
    dia 1, mas não custa ser tolerante.
    """
    presentes = {(p.data.year, p.data.month) for p in serie}
    return [m for m in esperados if (m.year, m.month) not in presentes]


# ---------------------------------------------------------------------------
# Orquestração das 3 camadas.
# ---------------------------------------------------------------------------
def valida_pontos(
    dados: list[dict],
    indicador: str,
    meses_esperados: list[date] | None = None,
) -> tuple[list[PontoSerie], list[str]]:
    """Valida uma lista de pontos crus contra as 3 camadas.

    Args:
        dados: pontos crus como as tools devolvem, ex. [{"data": "...",
            "valor": ...}, ...].
        indicador: chave do indicador (ex.: "ipca", "selic", "cambio") para a
            checagem de faixa.
        meses_esperados: se informado, ativa a camada 3 (completude).

    Returns:
        (validados, avisos):
          - validados: PontoSerie que passaram na forma E na faixa;
          - avisos: um texto por problema, dizendo o PORQUÊ. Preferimos deixar
            de fora um ponto suspeito a incluí-lo — falhar > número errado.
    """
    validados: list[PontoSerie] = []
    avisos: list[str] = []

    if indicador not in FAIXAS:
        avisos.append(
            f"indicador '{indicador}' não tem faixa plausível cadastrada em "
            f"FAIXAS — camada 2 (faixa) não pôde ser aplicada; revise."
        )

    for i, bruto in enumerate(dados):
        # Camada 1 — forma. Se não vira PontoSerie, é descartado com o motivo.
        try:
            ponto = PontoSerie.model_validate(bruto)
        except Exception as e:
            avisos.append(f"ponto #{i} descartado (forma inválida): {bruto} — {e}")
            continue

        # Camada 2 — faixa. Fora da faixa é descartado; não maquia.
        motivo = _fora_da_faixa(indicador, ponto.valor)
        if motivo is not None:
            avisos.append(f"ponto {ponto.data} descartado ({motivo})")
            continue

        validados.append(ponto)

    # Camada 3 — completude (opcional).
    if meses_esperados is not None:
        faltantes = faltam_meses(validados, meses_esperados)
        if faltantes:
            lista = ", ".join(d.strftime("%Y-%m") for d in faltantes)
            avisos.append(
                f"série de '{indicador}' incompleta: {len(faltantes)} mês(es) "
                f"esperado(s) ausente(s): {lista}"
            )

    return validados, avisos
