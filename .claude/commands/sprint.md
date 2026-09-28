---
description: Roda (ou retoma) uma sprint completa do Agente MAW e gera o PDF
argument-hint: "[--nova | --retomar]"
---

Você vai executar a sprint do Agente MAW de ponta a ponta. Leia `CLAUDE.md` primeiro. Não pergunte nada ao usuário: decisões que faltarem viram limitação declarada no PDF.

Use sempre `.venv/Scripts/python -m maw_agent` (abaixo, `MA`), sempre a partir da raiz do repositório `C:\Users\User\Agente MAW`. Cada comando imprime JSON; guarde só o resumo.

**Retomada.** Cada fase da CLI grava seu passo em `estado.json`. As fases de julgamento (subagentes) você mesmo marca, ao terminar cada uma, com `MA sprint marcar <passo> [--detalhe '<json>']` — os passos estão indicados abaixo. Na retomada, pule todo passo que aparece em `passos_concluidos` (ou em `MA sprint status`). Um passo que ficou em andamento é refeito.

## 0. Iniciar
`MA sprint iniciar $ARGUMENTS` — sem argumento (ou com `--retomar`) retoma a sprint em andamento, se houver; `--nova` começa outra. Anote `pasta` e `passos_concluidos`. Antes de qualquer coisa o comando restaura o `%APPDATA%\MAW` se uma execução anterior caiu no meio da suíte (`restauracoes`); se alguma restauração vier com `verificado: false`, siga assim mesmo — o PDF declara isso na capa.

## 1. Pré-voo e preparação
1. `MA sprint preflight`. Se falhar por bloqueio, pare e reporte.
2. `MA sprint preparar` (rode em segundo plano se demorar; pode levar minutos).

## 1b. Catalogar (em série)
Os catalogadores editam o mesmo `privado/catalogo/funcionalidades.yaml`: despache **um de cada vez**, nunca em paralelo. Primeiro o `main`, depois cada alvo que não seja `main` e não tenha `compartilha_com`. Cada `catalogador` acrescenta ao catálogo o que o alvo tem e o catálogo não, marcando `somente_em: [<alvo>]` quando a funcionalidade não existe no `main`, e roda `MA catalogo validar` no fim. Ao terminar cada um: `MA sprint marcar catalogar:<alvo>`. Depois do último: commit no privado e `MA sprint marcar catalogar`.

## 2. Compilar (em segundo plano)
`MA sprint compilar` — compila Release e Debug de cada alvo, um por vez. Enquanto compila, faça a fase 3.

## 3. Revisão de código (em paralelo, enquanto compila)
Leia `alvos.json` na pasta da sprint. Em todo despacho, o alvo é sempre o campo `nome` de `alvos.json` (nunca o nome da branch) e o commit é o `commit` desse alvo.

**Mapa área do catálogo → especialista** (o prefixo do `id` de cada item de `funcionalidades.yaml`):

| Especialista | Áreas do catálogo |
|---|---|
| `testador-motor` | transporte, gravacao, metronomo, desempenho |
| `testador-edicao` | edicao, selecao, marcadores |
| `testador-midi` | midi, piano-roll, instrumento |
| `testador-efeitos` | efeitos, rack, plugins |
| `testador-projeto` | projeto, importacao, exportacao |
| `testador-interface` | interface, atalhos, mixer, medidores |
| `testador-ia` | ia, servico |
| `guardiao-da-ideia` | documentacao, principio, saude |

**Achados abertos para reverificar:** em `privado/historico/achados.json`, os itens com `estado` em `novo`, `aberto`, `regressao` ou `nao_verificavel` cuja `ultimo.fonte` **não** é `build` nem `suite` (esses a consolidação reverifica sozinha, pelo build e pela suíte desta sprint). A área de um achado é o prefixo de `ultimo.item_catalogo`. Cada especialista recebe os abertos das suas áreas, com `id`, `titulo`, `item_catalogo`, `tipo`, `assinatura` e `alvos`.

- **Alvo `main`:** despache em paralelo os sete testadores e o `guardiao-da-ideia`, cada um com: nome do alvo, commit, caminho `work/alvos/main`, pasta da sprint, a lista das suas áreas do catálogo (tabela acima) e os achados abertos dessas áreas. Ao terminar cada um: `MA sprint marcar revisao:main:<agente>`.
- **Cada branch** (alvo que não é `main` e não tem `compartilha_com`): despache **um** `guardiao-da-ideia` com o diff `git -C work/espelho diff origin/main...<commit>` como foco e a instrução de revisar só o que a branch muda, em todas as áreas. Ao terminar: `MA sprint marcar revisao:<alvo>:guardiao-da-ideia`.
- **Cada alvo com `compartilha_com`** (mesma árvore de código de outro alvo): o código e a execução são os da origem, mas a documentação é revisada **por alvo**. Despache **um** `guardiao-da-ideia` focado em documentação: a documentação do alvo é lida com `git -C work/espelho show <commit>:<arquivo>` (liste com `git -C work/espelho ls-tree -r --name-only <commit>`) e conferida contra o código em `work/alvos/<origem>`. Ao terminar: `MA sprint marcar revisao:<alvo>:guardiao-da-ideia`.
- Cada subagente grava seus achados em `achados-brutos/` via `MA achado registrar` (a CLI recusa item de catálogo inexistente, alvo ou commit errados e nome de arquivo repetido — o subagente corrige e registra de novo), suas reverificações em `reverificacoes-<agente>.json` (id → corrigido|persiste|nao_verificavel) e o que não conseguiu verificar em `limitacoes-<agente>.json` (lista de textos). O `MA sprint consolidar` junta sozinho todos os `reverificacoes-*.json`; o PDF lê todos os `limitacoes-*.json`.

## 4. Suíte e benchmark
Quando a compilação terminar: `MA sprint suite` (em segundo plano). Ela só roda sobre o binário compilado nesta sprint, faz o backup do `%APPDATA%\MAW` só em volta da execução e o apaga depois da restauração verificada. Se a MAW do usuário estiver aberta, a suíte não roda (`recusado`) e isso vira `nao_testavel` no PDF; siga em frente.

## 5. Verificar
1. **Espere a suíte (fase 4) e TODOS os subagentes da fase 3 terminarem** antes de listar `achados-brutos/`. Quando os dois acabarem: `MA sprint marcar executar`.
2. Liste `achados-brutos/*.json`. Na retomada, pule os que já têm `vereditos/<mesmo nome>`.
3. Agrupe por arquivo e despache `advogado-do-diabo` (até 6 em paralelo, cada um com até 8 arquivos e a pasta da sprint). Cada um grava `<pasta da sprint>/vereditos/<nome exato do arquivo bruto>` — o mesmo nome, com a mesma extensão.
4. Quando todos terminarem: `MA sprint marcar verificar`. Achado sem veredito legível entra no PDF como provável e é declarado nas limitações.

## 6. Consolidar
`MA sprint consolidar`. Pode rodar de novo sem efeito colateral (parte sempre do histórico de antes da sprint).

## 7. Encerrar
`MA sprint encerrar` — confere que o ambiente foi restaurado e prova que a MAW ficou intocada. Rodar de novo devolve o que já foi registrado.

## 8. Textos
Despache `redator` com a pasta da sprint: ele lê `achados.json`, `alvos.json`, `builds/`, `suites/`, `benchmarks/`, `intocada.json`, `resultados.jsonl`, `erros_agente.json`, `derrubados.json`, `preflight.json` e `limitacoes-*.json`, e grava `textos.json`. Ao terminar: `MA sprint marcar textos`.

## 9. Relatório
1. `MA sprint relatorio` — gera o PDF (com `achados.json` anexado quando a consolidação rodou).
2. Commit no repositório **privado** (`git -C privado add relatorios historico && git -C privado commit -m "sprint NN: relatorio"`). Nunca no público.

## 10. Resposta final
Diga ao usuário: caminho do PDF, veredito, contagem por severidade, regressões e as principais limitações. Nada mais.
