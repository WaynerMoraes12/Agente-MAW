---
description: Roda (ou retoma) uma sprint completa do Agente MAW e gera o PDF
argument-hint: "[--nova]"
---

Você vai executar a sprint do Agente MAW de ponta a ponta. Leia `CLAUDE.md` primeiro. Não pergunte nada ao usuário: decisões que faltarem viram limitação declarada no PDF.

Use sempre `.venv/Scripts/python -m maw_agent` (abaixo, `MA`), sempre a partir da raiz do repositório `C:\Users\User\Agente MAW`. Cada comando imprime JSON; guarde só o resumo.

## 0. Iniciar
`MA sprint iniciar $ARGUMENTS` → anote `pasta` e `passos_concluidos`. Pule toda fase já concluída.

## 1. Pré-voo e preparação
1. `MA sprint preflight`. Se falhar por bloqueio, pare e reporte.
2. `MA sprint preparar` (rode em segundo plano se demorar; pode levar minutos).

## 1b. Catalogar
Despache `catalogador` uma vez por alvo que não seja `main` e não tenha `compartilha_com` (e uma vez para o `main`): ele acrescenta ao catálogo o que o alvo tem e o catálogo não, marcando `somente_em: [<alvo>]` quando a funcionalidade não existe no `main`, e rode `MA catalogo validar` no fim. Commit no privado.

## 2. Compilar (em segundo plano)
`MA sprint compilar` — compila Release e Debug de cada alvo, um por vez. Enquanto compila, faça a fase 3.

## 3. Revisão de código (em paralelo, enquanto compila)
Leia `alvos.json` na pasta da sprint.
- Para o alvo `main`: despache em paralelo os especialistas `testador-motor`, `testador-edicao`, `testador-midi`, `testador-efeitos`, `testador-projeto`, `testador-interface`, `testador-ia` e o `guardiao-da-ideia`, cada um com: nome do alvo, commit, caminho `work/alvos/main`, pasta da sprint e a lista de achados abertos do histórico da sua área (de `privado/historico/achados.json`) para reverificar.
- Para cada branch (alvo que não é `main` e não tem `compartilha_com`): despache **um** `guardiao-da-ideia` com o diff `git -C work/espelho diff origin/main...<commit>` como foco e a instrução de revisar só o que a branch muda, em todas as áreas.
- Cada subagente grava seus achados em `achados-brutos/` via `MA achado registrar` e suas reverificações num arquivo `reverificacoes-<agente>.json` (id → corrigido|persiste|nao_verificavel).
O `MA sprint consolidar` junta sozinho todos os `reverificacoes-*.json`.

## 4. Suíte e benchmark
Quando a compilação terminar: `MA sprint suite` (em segundo plano).

## 5. Verificar
Liste `achados-brutos/*.json`. Agrupe por arquivo e despache `advogado-do-diabo` (até 6 em paralelo, cada um com até 8 arquivos). Cada um grava `vereditos/<mesmo nome>.json`.

## 6. Consolidar
`MA sprint consolidar`.

## 7. Textos
Despache `redator` com a pasta da sprint: ele lê `achados.json`, `alvos.json`, `builds/`, `suites/` e grava `textos.json`.

## 8. Encerrar e relatório
1. `MA sprint encerrar` — restaura o ambiente e prova que a MAW ficou intocada.
2. `MA sprint relatorio` — gera o PDF.
3. Commit no repositório **privado** (`git -C privado add relatorios historico && git -C privado commit -m "sprint NN: relatorio"`). Nunca no público.

## 9. Resposta final
Diga ao usuário: caminho do PDF, veredito, contagem por severidade, regressões e as principais limitações. Nada mais.
