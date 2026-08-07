# rodar_ciclo.py
#
# A porta de entrada do job automático: roda o time e registra a previsão.
#
# Papel: o que o GitHub Actions chama depois de a sentinela dizer que há motivo.
# Existe para o workflow ter UM comando para chamar, em vez de encadear lógica
# em linha de shell no YAML — lógica em YAML é difícil de testar e ninguém
# consegue rodar na própria máquina.
#
# O que ele faz, em ordem:
#   1. roda o time (coleta -> previsão -> crítica -> redação);
#   2. publica data/resultado.json para o dashboard (via supervisor);
#   3. anota a previsão no caderno de acerto (data/previsoes.csv), amarrada ao
#      MÊS DE REFERÊNCIA que o calendário do IBGE informa.
#
# O passo 3 é o que fecha o fio que faltava: sem ele, o time preveria todo mês e
# o caderno de acerto continuaria vazio — e a sentinela nunca saberia que aquele
# mês já foi previsto, mandando prever de novo no dia seguinte.

from __future__ import annotations

import sys

from publicacao import ler_resultado
from registro import registrar_previsao, tem_previsao
from supervisor import rodar_time
from tools.calendario_ibge import CalendarioIndisponivel, proxima_divulgacao_ipca

PEDIDO_PADRAO = (
    "Colete o IPCA (variação mensal) desde janeiro de 2020 e preveja o "
    "próximo mês."
)


def _mes_de_referencia_previsto(indicador: str) -> dict | None:
    """De qual mês é a previsão que acabou de ser feita?

    O time prevê "o próximo valor da série", que é justamente o mês que o IBGE
    vai divulgar. Perguntamos ao calendário qual é esse mês, em vez de deduzir da
    data de hoje — em torno do dia 10 as duas coisas divergem, e errar aqui
    guardaria a previsão na linha do mês errado.
    """
    try:
        proxima = proxima_divulgacao_ipca()
    except CalendarioIndisponivel:
        return None
    if proxima["status"] != "ok":
        return None
    return proxima["referencia"]


def main() -> int:
    pedido = " ".join(sys.argv[1:]) or PEDIDO_PADRAO

    print(f"PEDIDO: {pedido}\n")
    estado = rodar_time(pedido, indicador="ipca", verboso=True)

    print("\n" + "=" * 70)
    print("RELATÓRIO FINAL")
    print("=" * 70)
    print(estado.get("relatorio") or "(nenhum relatório foi produzido)")

    avisos = estado.get("avisos") or []
    if avisos:
        print("\nAVISOS:")
        for aviso in avisos:
            print(f"  - {aviso}")

    publicacao = estado.get("publicacao") or {}
    if not publicacao.get("publicado"):
        # Rodada incompleta: o resultado anterior segue publicado, e o job
        # falha para aparecer vermelho no painel. Um sistema que "roda todo dia
        # com sucesso" sem produzir nada é pior do que um que falha visivelmente.
        print(f"\n::error::Rodada não publicada: {publicacao.get('motivo')}")
        return 1

    # --- registra a previsão no caderno de acerto ---
    referencia = _mes_de_referencia_previsto("ipca")
    if referencia is None:
        print("\n::warning::Não foi possível saber o mês de referência "
              "(calendário do IBGE indisponível) — a previsão foi publicada, "
              "mas NÃO entrou no caderno de acerto.")
        return 0

    if tem_previsao(referencia["ano"], referencia["mes"]):
        print(f"\nO mês de referência {referencia['rotulo']} já tinha previsão "
              f"registrada — o caderno não foi duplicado.")
        return 0

    linha = registrar_previsao(referencia["ano"], referencia["mes"],
                               estado.get("previsao") or {},
                               origem="agendada")
    if linha.get("status") == "ignorada":
        print(f"\n::warning::Previsão não registrada no caderno: "
              f"{linha['motivo']}")
    else:
        print(f"\nCaderno de acerto: previsão de {referencia['rotulo']} "
              f"registrada ({linha['valor_previsto']}%, "
              f"intervalo [{linha['intervalo_min']}, {linha['intervalo_max']}]).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
