# CLAUDE.md

Orientações para o Claude Code (claude.ai/code) ao trabalhar neste repositório.

## O que é

Sistema multi-agente de previsão do IPCA. Quatro agentes LangGraph (coletor,
previsor, crítico, redator) orquestrados por um supervisor determinístico, mais
uma sentinela que decide, todo dia e sem gastar token, se vale acordar o time.
Roda no GitHub Actions e publica num dashboard Streamlit.

Escopo deliberado: **uma variável só (IPCA)**. Nada de câmbio, PIB ou Selic.

Texto, comentários e prompts em **português (pt-BR)**, registro didático da
Análise Macro, sem maneirismos de escrita gerada por IA.

## A regra de ouro

**O agente decide e orquestra; o código puro executa.**

- **Código puro (sem LLM):** coleta de API, transformação numérica, cálculo de
  métrica, leitura de calendário, comparação de datas, persistência, gráfico.
- **Agente (LLM):** escolher qual ferramenta chamar a partir de um pedido em
  linguagem natural, criticar uma previsão, redigir o relatório, responder
  pergunta no chat.

Se você for tentado a colocar um agente para "somar dois números" ou "chamar uma
API", **pare e escreva uma função**. A sentinela e o módulo de calendário são os
exemplos vivos disso: são consulta, não julgamento.

Consequência que atravessa todo o código: **nenhum número é inventado**. Todo
dado vem de fonte oficial ou de modelo estatístico; falha vira aviso registrado.
E "não consegui verificar" nunca pode virar "não há novidade".

## Arquitetura

```
sentinela.py ──> supervisor.py ──> publicacao.py ──> dashboard.py
 (sem LLM)        (4 agentes)      (resultado.json)   (só lê e desenha)
```

- **`supervisor.py`** — o grafo. Cada agente entra como **subgrafo** (coletor e
  previsor têm laço interno agente ⇄ ferramentas). A aresta condicional
  `decidir_apos_critica` é **código puro**: ordenar quatro passos conhecidos não
  é trabalho de LLM. `TETO_TENTATIVAS = 3` limita o laço previsor ⇄ crítico;
  estourando, o relatório sai com a ressalva.
- **`state.py`** — o mural compartilhado. Só tipos simples e serializáveis: o
  estado inteiro vai para o checkpoint SQLite a cada passo, e objeto Pydantic ou
  `date` cru quebraria isso.
- **`tools/`** — o código puro exposto aos agentes como ferramenta. `TOOLS` é o
  que o coletor enxerga, `TOOLS_PREVISAO` o que o previsor enxerga.
  `calendario_ibge`, `ultimo_ponto` e `diagnostico` **não** entram em lista de
  tool: rodam antes, em código.

## Armadilhas já pagas (não repetir)

- **IPCA vs. IPCA-15.** No calendário do IBGE, o `produto_id` do IPCA cheio
  (9256) é prefixo exato do IPCA-15 (9260) e do IPCA-E (9262); o INPC (9258)
  ainda divulga na mesma data. Filtrar por texto captura os três. O filtro é por
  `produto_id` numérico e por igualdade — ver o comentário no topo de
  `tools/calendario_ibge.py`.
- **Gravação truncada.** `open("w")` trunca na hora; morrer no meio apaga o
  arquivo bom. Tudo que precisa sobreviver passa por
  `persistencia.escrever_atomico` (tmp + fsync + `os.replace`). Nunca escrever
  direto em `data/`.
- **Resposta do Gemini em partes.** `.content` nem sempre é string: às vezes vem
  como lista de partes, e `.content.strip()` quebra com `AttributeError`. Usar
  `texto.texto_da_resposta` (ou o equivalente em `conversa.py`).
- **API do IBGE comprimida.** O calendário às vezes responde gzip sem pedir, de
  forma intermitente. Tratado em `_decodificar`.
- **Pendente ≠ erro zero.** No caderno de acerto e no dashboard, previsão não
  conferida é "pendente", nunca 0,00 — senão vira acerto perfeito fantasma.

## O que sobrevive

O runner do Actions é destruído a cada execução. `persistencia.py` é a fonte
única sobre isso: `ARQUIVOS_QUE_SOBREVIVEM` (previsoes.csv, estado.json,
resultado.json, ipca.json) são commitados de volta; `checkpoints.sqlite`, `logs/`
e caches são descartáveis. Adicionar arquivo que precisa durar? Atualizar essa
lista, não só o `.gitignore`.

## Comandos

```bash
pip install -r requirements.txt
python sentinela.py            # checagem diária (não gasta token)
python sentinela.py 2026-08-10 # simula outra data
python rodar_ciclo.py          # roda o time inteiro
streamlit run dashboard.py     # o dashboard
python persistencia.py         # o que existe e o que sobreviveria
```

Não há suíte de testes nem linter configurados.

## Convenções

- Python 3.12 (ver `runtime.txt`). Na 3.14 a stack do langchain quebra.
- Versões travadas com `==` no `requirements.txt`, arquivo único servindo ao
  Actions e ao Posit Connect Cloud — o dashboard roda o time no botão e no chat,
  então precisa da stack completa.
- Chave sempre de variável de ambiente (`GOOGLE_API_KEY`). `load_dotenv()` não
  sobrescreve variável já definida, então `.env` local e segredo do servidor
  convivem. **Nunca commitar `.env`.**
- Ao adicionar série ou fonte nova em `tools/`, confirmar o código na API real
  antes de escrever — não usar código "de memória".
