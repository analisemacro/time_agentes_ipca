# dashboard.py
#
# A página que mostra o sistema. Rodar com:
#
#     streamlit run dashboard.py
#
# Papel: ler o que o job automático gravou e desenhar. Só isso.
#
# AO ABRIR, esta página não roda o time, não chama Gemini e não importa
# LangGraph. Ela lê dois arquivos locais — data/resultado.json e
# data/previsoes.csv — e desenha. É por isso que abre na hora.
#
# Duas ações do usuário, e SÓ elas, gastam chamada de API:
#
#   - o botão "rodar previsão agora" (barra lateral), que executa o time todo;
#   - cada pergunta no chat do rodapé.
#
# As duas avisam do custo na interface, e as duas importam a stack pesada
# (LangGraph, cliente do Gemini) DENTRO da função, não no topo do arquivo —
# quem só abre para olhar não paga esse import.
#
# Layout:
#
#   ┌───────────────────────────┬─────────────────────────────┐
#   │ gráfico: série + previsão │ tabela: histórico de acerto │
#   │ destacada com intervalo   │ (pendentes ≠ erro zero)     │
#   ├───────────────────────────┴─────────────────────────────┤
#   │ número em destaque · relatório · idade da última rodada │
#   └─────────────────────────────────────────────────────────┘

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from publicacao import ARQUIVO_RESULTADO, ler_resultado
from registro import ARQUIVO as ARQUIVO_PREVISOES
from registro import carregar_tabela, desempenho
from registro import historico as ler_historico

# Depois de quantos dias sem rodada nova a página desconfia.
#
# 40 dias: o IPCA é mensal, então o normal é uma rodada por mês, com folga de
# alguns dias. Passou disso, ou o job parou, ou a fonte está quebrada há tempo —
# e uma página que mostra número velho com cara de novo é pior que uma página
# fora do ar, porque ninguém percebe.
DIAS_ATE_DESCONFIAR = 40

# Paleta da casa (navy + ciano), a mesma dos slides e dos guias.
NAVY = "#1c2d4f"
CIANO = "#18a0d7"
LARANJA = "#e8833a"   # a previsão: o único ponto que não é dado observado
CINZA = "#8895a7"

st.set_page_config(page_title="IPCA — sistema multi-agente",
                   page_icon="📈", layout="wide")


# ---------------------------------------------------------------------------
# Leitura. Cacheada por caminho+mtime: reler dois arquivos pequenos é barato,
# mas assim um F5 não relê nada à toa, e a página continua atualizando quando o
# job grava por cima (o mtime muda e o cache cai sozinho).
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _carregar_resultado(_mtime: float) -> dict | None:
    return ler_resultado()


@st.cache_data(show_spinner=False)
def _carregar_historico(_mtime: float) -> pd.DataFrame:
    return carregar_tabela()


def _mtime(caminho) -> float:
    return caminho.stat().st_mtime if caminho.exists() else 0.0


def _proximo_mes(data_iso: str) -> date:
    """O mês seguinte ao último ponto da série — é o mês que a previsão cobre."""
    u = date.fromisoformat(data_iso)
    return date(u.year + (u.month == 12), (u.month % 12) + 1, 1)


def _dias_desde(data_iso: str) -> int | None:
    try:
        return (date.today() - date.fromisoformat(data_iso)).days
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# A página ainda não tem o que mostrar.
# ---------------------------------------------------------------------------
def _pagina_sem_resultado() -> None:
    """Explica que ainda não houve rodada, em vez de quebrar com KeyError.

    Este caso acontece de verdade: no primeiro deploy, antes de o job rodar pela
    primeira vez. Uma página que estoura aqui parece sistema quebrado quando na
    verdade é sistema novo.
    """
    st.title("📈 IPCA — sistema multi-agente")
    st.info(
        "**Nenhuma rodada foi publicada ainda.**\n\n"
        "Esta página lê o arquivo que o job automático grava ao fim de cada "
        "rodada. Ele ainda não existe:\n\n"
        f"`{ARQUIVO_RESULTADO}`\n\n"
        "Isso é esperado antes da primeira execução. Para gerar o arquivo:\n\n"
        "- no GitHub: aba **Actions** → *IPCA — previsão automática* → "
        "**Run workflow**, marcando `forcar_time`; ou\n"
        "- na sua máquina: `python rodar_ciclo.py`\n\n"
        "A página não roda o time sozinha de propósito: quem gasta chamada de "
        "modelo é o job, não quem abre o dashboard."
    )

    historico = _carregar_historico(_mtime(ARQUIVO_PREVISOES))
    if not historico.empty:
        st.divider()
        st.caption("Já existe histórico de previsões registrado:")
        st.dataframe(historico, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Gráfico: a série observada + o ponto previsto com a faixa do intervalo.
# ---------------------------------------------------------------------------
def _grafico(resultado: dict) -> go.Figure:
    serie = pd.DataFrame(resultado["serie"])
    serie["data"] = pd.to_datetime(serie["data"])

    previsao = resultado["previsao"]
    data_prev = pd.Timestamp(_proximo_mes(resultado["ultima_data_serie"]))
    ultimo = serie.iloc[-1]

    fig = go.Figure()

    # A faixa do intervalo. Desenhada do último ponto observado até o previsto,
    # para o intervalo "sair" da série em vez de flutuar solto no fim do gráfico.
    minimo, maximo = previsao.get("intervalo_min"), previsao.get("intervalo_max")
    if minimo is not None and maximo is not None:
        fig.add_trace(go.Scatter(
            x=[ultimo["data"], data_prev, data_prev, ultimo["data"]],
            y=[ultimo["valor"], maximo, minimo, ultimo["valor"]],
            fill="toself", fillcolor="rgba(232, 131, 58, 0.18)",
            line={"width": 0}, hoverinfo="skip",
            name=f"Intervalo {int((previsao.get('intervalo_nivel') or 0.95) * 100)}%",
        ))

    # A série observada.
    fig.add_trace(go.Scatter(
        x=serie["data"], y=serie["valor"], mode="lines",
        line={"color": NAVY, "width": 2}, name="IPCA observado",
        hovertemplate="%{x|%b/%Y}: %{y:.2f}%<extra></extra>",
    ))

    # A ligação entre o último observado e o previsto, tracejada: o trecho que
    # NÃO é dado, e a linha cheia não pode sugerir que é.
    fig.add_trace(go.Scatter(
        x=[ultimo["data"], data_prev], y=[ultimo["valor"], previsao["valor"]],
        mode="lines", line={"color": LARANJA, "width": 2, "dash": "dot"},
        showlegend=False, hoverinfo="skip",
    ))

    # O ponto previsto, destacado.
    fig.add_trace(go.Scatter(
        x=[data_prev], y=[previsao["valor"]], mode="markers+text",
        marker={"color": LARANJA, "size": 13,
                "line": {"color": "white", "width": 2}},
        text=[f" {previsao['valor']:.2f}%"], textposition="middle right",
        textfont={"color": LARANJA, "size": 14},
        name="Previsão",
        hovertemplate=(f"Previsão {data_prev:%b/%Y}: "
                       f"{previsao['valor']:.2f}%<extra></extra>"),
    ))

    fig.update_layout(
        height=430, margin={"l": 10, "r": 10, "t": 30, "b": 10},
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
        yaxis={"title": "% no mês", "zeroline": True,
               "zerolinecolor": "rgba(0,0,0,0.15)"},
        xaxis={"title": None},
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


# ---------------------------------------------------------------------------
# Tabela: o histórico de acerto. Pendente ≠ erro zero.
# ---------------------------------------------------------------------------
def _tabela_historico(historico: pd.DataFrame) -> pd.DataFrame:
    """Formata o caderno para leitura, sem inventar número onde não há.

    O cuidado central: uma previsão ainda não conferida tem valor_real vazio.
    Preencher com 0 faria a página mostrar "erro 0,00" — ou seja, acerto
    perfeito — para uma previsão que ninguém conferiu ainda. Aqui isso vira o
    texto "pendente", que não se confunde com número.
    """
    tabela = pd.DataFrame({
        "Mês previsto": historico["referencia"],
        "Previsto em": historico["previsto_em"],
        "Sistema": historico["valor_previsto"].map(
            lambda v: f"{float(v):.2f}%" if pd.notna(v) else "—"),
        "IBGE": historico["valor_real"].map(
            lambda v: f"{float(v):.2f}%" if pd.notna(v) else "pendente"),
        "Erro": historico["erro"].map(
            lambda v: f"{float(v):+.2f}" if pd.notna(v) else "pendente"),
        # Distingue o que o sistema fez sozinho do que foi disparado no botão.
        # Linha antiga (anterior à coluna) fica com "—" em vez de sumir.
        "Origem": (historico["origem"].fillna("—").replace("", "—")
                   if "origem" in historico.columns else "—"),
    })
    # Mais recente primeiro: é o que se quer ver ao abrir.
    return tabela.iloc[::-1].reset_index(drop=True)


def _estilo_pendente(valor):
    """Deixa 'pendente' visualmente distinto de um número de erro."""
    if valor == "pendente":
        return f"color: {CINZA}; font-style: italic"
    if isinstance(valor, str) and valor.startswith("+"):
        return "color: #c0392b"      # superestimou
    if isinstance(valor, str) and valor.startswith("-"):
        return "color: #1e8449"      # subestimou
    return ""


# ---------------------------------------------------------------------------
# Botão "rodar previsão agora" — a ÚNICA parte da página que roda o time.
#
# Fica na barra lateral, separado do resto, porque é a única coisa aqui que
# custa dinheiro e demora minutos. O aviso de custo é obrigatório: quem abre um
# dashboard espera que clicar seja de graça, e aqui não é.
# ---------------------------------------------------------------------------
def _barra_lateral() -> None:
    with st.sidebar:
        st.header("Rodar agora")
        st.caption(
            "Executa o time inteiro (coleta → previsão → crítica → redação) "
            "na hora, sem esperar o agendamento."
        )
        st.warning(
            "💰 **Gasta chamadas de API.** Uma rodada usa várias chamadas ao "
            "Gemini e leva alguns minutos. O job automático já faz isso "
            "sozinho quando chega a hora — use este botão só para ver o número "
            "antes.",
            icon="⚠️",
        )

        if st.button("▶️ Rodar previsão agora", type="primary",
                     use_container_width=True):
            _rodar_time_agora()

        ultima = st.session_state.get("ultima_rodada_manual")
        if ultima:
            st.divider()
            if ultima["ok"]:
                st.success(ultima["mensagem"])
            else:
                st.error(ultima["mensagem"])


def _rodar_time_agora() -> None:
    """Roda o time na hora e registra no histórico como rodada MANUAL.

    Faz o mesmo que rodar_ciclo.py (o caminho do job automático), com uma
    diferença: marca `origem="manual"` no caderno. Sem essa marca, uma rodada
    disparada na mão depois da divulgação — quando o número já é conhecido — se
    misturaria às automáticas e inflaria o acerto medido do sistema.
    """
    # Importado aqui dentro, não no topo: assim quem só abre a página para olhar
    # não carrega LangGraph nem o cliente do Gemini. É o que mantém a abertura
    # instantânea.
    from registro import registrar_previsao, tem_previsao
    from rodar_ciclo import PEDIDO_PADRAO, _mes_de_referencia_previsto
    from supervisor import rodar_time

    with st.status("Rodando o time…", expanded=True) as status:
        try:
            st.write("Acordando os agentes (isso leva alguns minutos)…")
            estado = rodar_time(PEDIDO_PADRAO, indicador="ipca", verboso=False)

            publicacao = estado.get("publicacao") or {}
            if not publicacao.get("publicado"):
                status.update(label="Rodada incompleta", state="error")
                st.session_state["ultima_rodada_manual"] = {
                    "ok": False,
                    "mensagem": (f"Rodada não publicada: "
                                 f"{publicacao.get('motivo')} "
                                 f"O resultado anterior foi preservado."),
                }
                return

            st.write("Resultado publicado. Registrando no histórico…")
            referencia = _mes_de_referencia_previsto("ipca")
            previsao = estado.get("previsao") or {}

            if referencia is None:
                nota = ("A previsão foi publicada, mas o calendário do IBGE "
                        "não respondeu — ela NÃO entrou no caderno de acerto.")
            elif tem_previsao(referencia["ano"], referencia["mes"]):
                nota = (f"{referencia['rotulo']} já tinha previsão registrada; "
                        f"o caderno não foi duplicado.")
            else:
                linha = registrar_previsao(
                    referencia["ano"], referencia["mes"], previsao,
                    origem="manual",
                )
                nota = (f"Registrada no caderno como rodada **manual** "
                        f"({referencia['rotulo']})."
                        if linha.get("status") != "ignorada"
                        else f"Não registrada: {linha['motivo']}")

            aprovado = (estado.get("parecer") or {}).get("decisao") == "aprova"
            status.update(label="Rodada concluída", state="complete")
            st.session_state["ultima_rodada_manual"] = {
                "ok": True,
                "mensagem": (
                    f"Previsão: **{previsao.get('valor')}%** "
                    f"({'aprovada' if aprovado else 'NÃO aprovada'} pelo "
                    f"crítico). {nota}"
                ),
            }

            # Os arquivos mudaram; o cache é por mtime, então cai sozinho.
            st.rerun()

        except Exception as erro:
            # Cota estourada, chave ausente, API fora. A página não pode morrer
            # junto — o resultado anterior continua publicado e visível.
            status.update(label="A rodada falhou", state="error")
            st.session_state["ultima_rodada_manual"] = {
                "ok": False,
                "mensagem": (f"A rodada falhou (`{type(erro).__name__}`): "
                             f"{erro}\n\nO resultado anterior continua "
                             f"publicado."),
            }


# ---------------------------------------------------------------------------
# Conversa com o agente. A outra parte que gasta API.
# ---------------------------------------------------------------------------
def _secao_conversa(resultado: dict | None) -> None:
    st.subheader("Perguntar ao agente")
    st.caption(
        "💰 **Cada pergunta gasta uma chamada de API.** O agente responde "
        "apenas com o que está nos arquivos do projeto — a série, a previsão "
        "atual e o histórico de erros. Se a pergunta não puder ser respondida "
        "com esses dados, ele diz que não sabe, em vez de inventar número."
    )

    if "conversa" not in st.session_state:
        st.session_state["conversa"] = []

    for msg in st.session_state["conversa"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    pergunta = st.chat_input("Ex.: o sistema tem acertado? por que o intervalo "
                             "é tão largo?")
    if not pergunta:
        return

    st.session_state["conversa"].append({"role": "user", "content": pergunta})
    with st.chat_message("user"):
        st.markdown(pergunta)

    with st.chat_message("assistant"):
        with st.spinner("Consultando os dados do projeto…"):
            from conversa import responder

            resposta = responder(
                pergunta,
                resultado,
                ler_historico(),
                # Sem a última troca (a pergunta que acabou de entrar), senão
                # ela apareceria duplicada no histórico da conversa.
                conversa_anterior=st.session_state["conversa"][:-1],
            )
        st.markdown(resposta)

    st.session_state["conversa"].append({"role": "assistant",
                                         "content": resposta})


# ---------------------------------------------------------------------------
# A página.
# ---------------------------------------------------------------------------
def main() -> None:
    resultado = _carregar_resultado(_mtime(ARQUIVO_RESULTADO))

    # A barra lateral aparece SEMPRE, inclusive antes da primeira rodada — é
    # justamente aí que o botão é mais útil, para gerar o primeiro resultado
    # sem esperar o agendamento.
    _barra_lateral()

    if resultado is None:
        _pagina_sem_resultado()
        st.divider()
        _secao_conversa(None)
        return

    if resultado.get("_incompativel"):
        st.error(f"⚠️ {resultado['_incompativel']} A página pode estar lendo "
                 f"campos que não existem mais. Atualize o dashboard.")

    historico = _carregar_historico(_mtime(ARQUIVO_PREVISOES))
    previsao = resultado["previsao"]
    parecer = resultado["parecer"]

    st.title("📈 IPCA — sistema multi-agente")

    # --- Aviso de rodada velha, antes de tudo -----------------------------
    # Vem primeiro de propósito: se o dado está velho, isso muda como se lê
    # todo o resto da página.
    dias = _dias_desde(resultado["rodada_em"])
    if dias is not None and dias > DIAS_ATE_DESCONFIAR:
        st.error(
            f"⚠️ **A última rodada foi há {dias} dias** "
            f"({resultado['rodada_em']}). O IPCA é mensal, então o normal é uma "
            f"rodada por mês. O sistema pode estar com problema — verifique o "
            f"job automático (aba Actions) e as fontes de dados. "
            f"**Os números abaixo estão desatualizados.**"
        )

    # --- Aviso de previsão não aprovada -----------------------------------
    # O crítico pode reprovar e o relatório sair mesmo assim (quando o teto de
    # tentativas estoura). Mostrar o número sem esta ressalva apresentaria como
    # validado algo que foi recusado.
    if resultado.get("alerta"):
        st.warning(f"🚩 **{resultado['alerta']['texto']}**")

    # --- Gráfico | Tabela --------------------------------------------------
    esquerda, direita = st.columns([3, 2], gap="large")

    with esquerda:
        st.subheader("Série e previsão")
        st.plotly_chart(_grafico(resultado), use_container_width=True)

    with direita:
        st.subheader("O sistema acerta?")
        if historico.empty:
            st.caption("Nenhuma previsão registrada ainda. A tabela se preenche "
                       "a cada rodada.")
        else:
            tabela = _tabela_historico(historico)
            st.dataframe(
                tabela.style.map(_estilo_pendente, subset=["IBGE", "Erro"]),
                use_container_width=True, hide_index=True, height=380,
            )

            resumo = desempenho()
            if resumo.get("n"):
                a, b, c = st.columns(3)
                a.metric("Conferidas", resumo["n"])
                b.metric("Erro médio (abs.)",
                         f"{resumo['erro_absoluto_medio']:.2f} p.p.",
                         help="Tamanho típico do erro, sem compensar sinal.")
                if resumo.get("cobertura") is not None:
                    c.metric("Dentro do intervalo",
                             f"{resumo['cobertura'] * 100:.0f}%",
                             help=("Quantas vezes o valor real caiu dentro do "
                                   "intervalo previsto. Um intervalo de 95% "
                                   "honesto deveria acertar perto disso."))
            else:
                st.caption("Nenhuma previsão foi conferida contra o valor real "
                           "ainda — o erro aparece quando o IBGE publica.")

    st.divider()

    # --- O número em destaque ---------------------------------------------
    mes_previsto = _proximo_mes(resultado["ultima_data_serie"])
    MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
             "agosto", "setembro", "outubro", "novembro", "dezembro"]
    rotulo_mes = f"{MESES[mes_previsto.month - 1]} de {mes_previsto.year}"

    col_num, col_int, col_mod, col_sel = st.columns([2, 2, 2, 2])
    col_num.metric(f"Previsão para {rotulo_mes}", f"{previsao['valor']:.2f}%")

    if previsao.get("intervalo_min") is not None:
        nivel = int((previsao.get("intervalo_nivel") or 0.95) * 100)
        col_int.metric(f"Intervalo ({nivel}%)",
                       f"{previsao['intervalo_min']:.2f} a "
                       f"{previsao['intervalo_max']:.2f}%")

    col_mod.metric("Modelo", previsao.get("modelo") or "—")
    col_sel.metric("Parecer do crítico",
                   "aprovado" if parecer["aprovado"] else "não aprovado")

    # --- O relatório do redator -------------------------------------------
    st.subheader("Relatório")
    st.markdown(resultado["relatorio"])

    # --- Detalhes de auditoria, recolhidos --------------------------------
    with st.expander("Detalhes da rodada"):
        st.write(f"**Parecer:** {parecer['decisao']} "
                 f"(confiança: {parecer.get('confianca') or '—'}, "
                 f"origem: {parecer.get('origem') or '—'})")
        if parecer.get("motivos"):
            st.write("**Motivos do crítico:**")
            for motivo in parecer["motivos"]:
                st.write(f"- {motivo}")
        st.write(f"**Tentativas de previsão:** "
                 f"{resultado.get('tentativas_de_previsao', '—')}")
        if previsao.get("observacoes"):
            st.write(f"**Observações do modelo:** {previsao['observacoes']}")
        if resultado.get("avisos"):
            st.write("**Avisos da rodada:**")
            for aviso in resultado["avisos"]:
                st.write(f"- {aviso}")
        st.caption(f"Série: {len(resultado['serie'])} pontos, até "
                   f"{resultado['ultima_data_serie']}. "
                   f"Contrato v{resultado.get('versao_contrato')}.")

    # --- Conversa com o agente --------------------------------------------
    st.divider()
    _secao_conversa(resultado)

    # --- Rodapé: quando isto foi atualizado -------------------------------
    st.divider()
    idade = ("hoje" if dias == 0 else
             "ontem" if dias == 1 else
             f"há {dias} dias" if dias is not None else "data desconhecida")
    st.caption(
        f"Última rodada: **{resultado['rodada_em']}** ({idade}) · "
        f"série do IPCA até {resultado['ultima_data_serie']} · "
        f"a página abre lendo apenas os arquivos gravados pelo job "
        f"automático; o botão de rodar e o chat, esses sim, gastam API."
    )


if __name__ == "__main__":
    main()
