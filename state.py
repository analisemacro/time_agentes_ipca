# state.py
#
# Estado do grafo LangGraph — o "mural" onde cada nó ACRESCENTA informação, sem
# apagar o que os outros escreveram.
#
# Papel: definir o objeto de estado que trafega entre os nós. Começou servindo só
# ao Agente de Coleta (agente, ferramentas, guardar) e agora carrega também o que
# o TIME de agentes troca entre si: previsão, parecer do crítico, contagem de
# tentativas e relatório final.
#
# O campo `messages` usa add_messages, então cada nó adiciona mensagens à conversa
# em vez de sobrescrevê-la — é a memória de trabalho do time.
#
# Regra que vale para todo campo daqui: só tipo SIMPLES e serializável (dict,
# list, str, float, int, bool). O estado inteiro é gravado no checkpoint SQLite a
# cada passo do grafo; objeto Pydantic ou date cru quebrariam a serialização.
# Por isso data vira string ISO e schema Pydantic vira dict com .model_dump().

from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class _CamposDaColeta(TypedDict):
    """Os campos que a coleta sempre preenche — OBRIGATÓRIOS, como antes.

    Ficam num TypedDict separado só para continuarem exigidos: os campos do time
    de agentes precisam ser opcionais (ninguém tem previsão antes de prever), e
    `total=False` no mesmo bloco afrouxaria estes quatro junto.
    """

    messages: Annotated[list, add_messages]
    indicador: str
    validados: list[dict]
    avisos: list[str]


class EstadoColeta(_CamposDaColeta, total=False):
    """Mural compartilhado entre os nós do grafo.

    Campos da coleta (os que já existiam, herdados de _CamposDaColeta e ainda
    obrigatórios):

    - messages: a conversa (humano, IA, chamadas/retornos de tool). Anotada com
      add_messages: cada nó ACRESCENTA, nunca sobrescreve.
    - indicador: qual indicador está sendo coletado (ex.: "ipca"), usado pela
      validação de faixa.
    - validados: pontos que passaram na validação, como dicts serializáveis
      {"data": "AAAA-MM-DD", "valor": float} — dict simples, e não objeto
      Pydantic, porque o estado inteiro é serializado no checkpoint SQLite.
      É esta a série que o previsor consome.
    - avisos: o que NÃO passou, cada aviso com o porquê — nada é maquiado.

    Campos do time de agentes (novos). Todos OPCIONAIS: o grafo de coleta
    continua rodando sem nenhum deles preenchido, e cada agente só escreve no
    campo que é dele.

    - previsao: o que o Agente Previsor produziu, como dict serializável. O
      formato fica a cargo do previsor; o combinado é que traga pelo menos o
      número previsto, o horizonte e como chegou nele.
    - parecer: o veredito do Agente Crítico sobre a `previsao` acima, também
      como dict. O combinado é trazer a decisão (aprova/ajusta/rejeita) e os
      motivos — é o que o coordenador lê para decidir se refaz a previsão.
    - tentativas: quantas rodadas de previsão já foram submetidas ao crítico.
      É o freio do laço previsor <-> crítico: sem um contador aqui, uma previsão
      que o crítico nunca aprova faz o grafo girar para sempre. Quem incrementa
      é o nó do previsor; quem compara com o teto é o coordenador.
    - relatorio: o texto final do Agente Redator, em linguagem natural. Só é
      preenchido depois de um parecer aprovado — é a última coisa a entrar no
      mural.
    """

    # --- time de agentes (novo; os da coleta vêm de _CamposDaColeta) ---
    previsao: dict
    parecer: dict
    tentativas: int
    relatorio: str
