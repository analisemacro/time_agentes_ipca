# Time de agentes — previsão do IPCA

Sistema multi-agente que prevê a inflação brasileira medida pelo IPCA, roda
sozinho todo dia e publica o resultado num dashboard.

Não é um chatbot que opina sobre inflação. É um time de quatro agentes com
responsabilidades separadas, orquestrado de forma determinística, onde **o
número sai de um modelo estatístico e o agente nunca inventa dado**.

## Como funciona

```
                    sentinela (todo dia, sem LLM)
                              │
              ┌───────────────┴───────────────┐
              │                               │
     "falta pouco para                "a série avançou?"
      a divulgação?"                          │
              │                               │
              ▼                               ▼
    ┌─────────────────────────────────┐   registra o valor real
    │  coleta → previsão → crítica    │   ao lado do previsto
    │              ↑         │        │
    │              └─reprova─┘        │
    │                  ↓ aprova       │
    │              redação            │
    └─────────────────────────────────┘
                    │
                    ▼
            data/resultado.json  →  dashboard
```

**A sentinela** roda todo dia e custa duas requisições HTTP, sem nenhuma chamada
de modelo de linguagem. Ela responde duas perguntas: falta pouco para a
divulgação do IBGE (e ainda não previmos aquele mês)? A série avançou (e agora
há um valor real para comparar com o que foi previsto)? Na maioria dos dias a
resposta é "nada a fazer", e o time caro nem é acionado.

**O time** só acorda quando há motivo:

| Agente | O que faz |
|---|---|
| **Coletor** | escolhe a ferramenta de coleta a partir do pedido em linguagem natural; quem fala com BCB/SGS e IBGE/SIDRA é código puro |
| **Previsor** | roda um ARIMA sobre a série e decide como reagir ao resultado — o número vem do modelo, nunca do LLM |
| **Crítico** | tenta refutar a previsão: está coerente com os meses recentes? o intervalo é estreito demais? aprova ou rejeita, com motivos |
| **Redator** | escreve o relatório em português a partir do que foi aprovado |

Se o crítico reprova, o previsor tenta de novo (até 3 vezes). Estourando o teto,
o relatório sai **com a ressalva visível** — publicar "não aprovado, e por isto"
é mais útil que não publicar.

## A regra que atravessa o projeto

> O agente decide e orquestra; o código puro executa.

Um agente só se justifica onde há julgamento, ambiguidade ou linguagem natural.
Baixar uma série, calcular uma média, ler um calendário e comparar duas datas são
tarefas determinísticas — e viram função Python, mais barata, mais rápida e
testável.

Na prática: todo número vem de uma fonte oficial ou de um modelo estatístico.
Falha vira aviso registrado, nunca valor inventado. Se a fonte não responde, o
sistema diz "não deu para verificar" em vez de "não há novidade" — porque
confundir as duas é como um sistema fica semanas quebrado sem ninguém notar.

## Rodar na sua máquina

```bash
pip install -r requirements.txt
cp .env.example .env        # e ponha a sua chave do Gemini

python sentinela.py         # a checagem diária (não gasta token)
python rodar_ciclo.py       # roda o time inteiro
streamlit run dashboard.py  # o dashboard
```

A chave sai de graça em [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

## O dashboard

Abre instantaneamente porque só lê dois arquivos e desenha — não roda o time nem
chama modelo nenhum ao abrir. Mostra a série com a previsão destacada e a faixa
do intervalo, o histórico de acerto (previsto × realizado × erro, com as
pendentes marcadas como pendentes, não como erro zero), o relatório e a idade da
última rodada.

Duas ações **gastam** chamada de API, e avisam disso na interface: o botão
"rodar previsão agora" e o chat, onde um agente responde perguntas usando apenas
os dados do projeto — e diz que não sabe quando a pergunta cai fora deles.

## Automação

O workflow em `.github/workflows/ipca.yml` acorda às 6h (horário de Brasília),
roda a sentinela e só aciona o time quando há motivo. No fim, commita de volta os
arquivos que precisam sobreviver — o runner do GitHub é destruído a cada
execução, e o repositório é o único lugar onde a memória do sistema dura.

Para funcionar, cadastre a chave em **Settings → Secrets and variables →
Actions**, com o nome `GOOGLE_API_KEY`.

## Estrutura

```
sentinela.py       decide se vale acordar o time (código puro)
supervisor.py      o grafo LangGraph que amarra os quatro agentes
coletor.py         previsor.py   critico.py   redator.py
rodar_ciclo.py     porta de entrada do job automático
dashboard.py       a página em Streamlit
conversa.py        o agente do chat

registro.py        caderno de acerto: previsto × realizado (data/previsoes.csv)
publicacao.py      o contrato com o dashboard (data/resultado.json)
persistencia.py    o que sobrevive à máquina ser destruída + gravação atômica
memoria.py         a série coletada, com merge por data

tools/             o código puro que os agentes chamam como ferramenta
prompts/           os prompts de sistema de cada agente
data/              série, previsões, estado e resultado publicado
```

## Stack

Python 3.12, LangGraph, Gemini (Flash Lite), statsmodels, Streamlit, Plotly.

---

Material da **Imersão Agentes Autônomos de IA** da
[Análise Macro](https://analisemacro.com.br).
