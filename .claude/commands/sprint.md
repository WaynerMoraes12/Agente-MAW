---
description: Roda (ou retoma) uma sprint completa do Roadie (agente de testes da MAW) e gera o PDF
argument-hint: "[--nova | --retomar] [--ate revisao] [--noturno]"
---

Você vai executar a sprint do Roadie de ponta a ponta. Leia `CLAUDE.md` primeiro. Não pergunte nada ao usuário: decisões que faltarem viram limitação declarada no PDF.

Use sempre `.venv/Scripts/python -m maw_agent` (abaixo, `MA`), sempre a partir da raiz do repositório `C:\Users\User\Agente MAW`. Cada comando imprime JSON; guarde só o resumo. Rode os comandos pela ferramenta **Bash**, escritos exatamente como aqui (`.venv/Scripts/python -m maw_agent ...`, `git -C privado ...`, `git -C work/espelho ...`): na execução noturna as permissões (`.claude/settings.json`) só liberam esses, e o que não estiver liberado é negado sem pergunta.

**Argumentos** (`$ARGUMENTS`). `--nova` ou `--retomar` escolhem a sprint e são os únicos que vão para a CLI. `--ate revisao`: pare depois da fase 3 (veja o fim da fase 3). `--noturno`: você foi chamado pelo script noturno (`ferramentas/noturno.ps1`), que roda as fases determinísticas fora de você — veja abaixo.

**Execução noturna (`--noturno`).** O script já rodou `sprint iniciar`, `preflight` e `preparar`, e roda sozinho `compilar`, `suite`, `e2e`, `servico` e `calibrar` — às vezes ao mesmo tempo que você. Então:
- não rode nenhuma dessas fases, nem `MA sprint iniciar` (a retomada restauraria o `%APPDATA%\MAW` no meio da suíte). Uma dessas fases que falhou ou não aparece concluída já foi registrada pelo script: siga com o que houver;
- no passo 0, use `MA sprint status` para saber a sprint e os passos concluídos; a pasta da sprint é `privado/relatorios/<sprint>`;
- não há ninguém para responder: permissão negada ou ferramenta indisponível vira limitação (`MA noturno limitacao --origem julgamento "<o que não deu e por quê>"`) e você segue.

**Subagentes em primeiro plano.** Despache os subagentes sempre em primeiro plano (nunca com `run_in_background`). Quando um passo manda despachar em paralelo, ponha todos os despachos **na mesma mensagem**: eles rodam juntos e você recebe todos os resultados. Com `--noturno`, nada fica em segundo plano: um comando Bash deixado em segundo plano morre logo depois da resposta final do `claude -p` (um subagente em segundo plano não seria cortado por inatividade, mas em primeiro plano você sabe quando cada um terminou).

**Retomada.** Cada fase da CLI grava seu passo em `estado.json`. As fases de julgamento (subagentes) você mesmo marca, ao terminar cada uma, com `MA sprint marcar <passo> [--detalhe '<json>']` — os passos estão indicados abaixo. Na retomada, pule todo passo que aparece em `passos_concluidos` (ou em `MA sprint status`). Um passo que ficou em andamento é refeito.

## 0. Iniciar
`MA sprint iniciar --nova` se `$ARGUMENTS` tiver `--nova`; senão `MA sprint iniciar --retomar` (retoma a sprint em andamento, se houver). Com `--noturno`, em vez disso: `MA sprint status`. Anote `pasta` e `passos_concluidos`. Antes de qualquer coisa o `iniciar` restaura o `%APPDATA%\MAW` se uma execução anterior caiu no meio da suíte (`restauracoes`); se alguma restauração vier com `verificado: false`, siga assim mesmo — o PDF declara isso na capa.

## 1. Pré-voo e preparação
(Com `--noturno`, pule: o script já fez.)
1. `MA sprint preflight`. Se falhar por bloqueio, pare e reporte.
2. `MA sprint preparar` (rode em segundo plano se demorar; pode levar minutos).

## 1b. Catalogar (em série)
Os catalogadores editam o mesmo `privado/catalogo/funcionalidades.yaml`: despache **um de cada vez**, nunca em paralelo. Primeiro o `main`, depois cada alvo que não seja `main` e não tenha `compartilha_com`. Cada `catalogador` acrescenta ao catálogo o que o alvo tem e o catálogo não, marcando `somente_em: [<alvo>]` quando a funcionalidade não existe no `main`, e roda `MA catalogo validar` no fim. Ao terminar cada um: `MA sprint marcar catalogar:<alvo>`. Depois do último: commit no privado e `MA sprint marcar catalogar`.

## 2. Compilar (em segundo plano)
`MA sprint compilar` — compila Release e Debug de cada alvo, um por vez. Enquanto compila, faça a fase 3. (Com `--noturno`, pule: o script compila enquanto você faz 1b e 3.)

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

**Achados abertos para reverificar:** em `privado/historico/achados.json`, os itens com `estado` em `novo`, `aberto`, `regressao` ou `nao_verificavel` cuja `ultimo.fonte` **não** é `build`, `suite`, `sonda`, `e2e` nem `servico` (os três primeiros a consolidação reverifica sozinha, pelo build e pela suíte desta sprint; `e2e` e `servico` são refeitos pelas próprias fases a cada sprint e não cabem a quem só lê código). A área de um achado é o prefixo de `ultimo.item_catalogo`. Cada especialista recebe os abertos das suas áreas, com `id`, `titulo`, `item_catalogo`, `tipo`, `assinatura` e `alvos`. `corrigido` e `alvo_removido` (achado de uma branch já apagada do GitHub, marcado numa sprint anterior) **não** vão. Um achado aberto cujos alvos não estão em `alvos.json` (a branch foi apagada ou mesclada) vai para os especialistas do `main`: o código da branch pode ter entrado no main, e eles o reverificam contra `work/alvos/main` — `persiste` faz a consolidação acrescentar o `main` aos alvos do achado; sem reverificação, ele vira `alvo_removido` se a branch sumiu do GitHub.

- **Alvo `main`:** despache em paralelo — os oito na mesma mensagem, em primeiro plano — os sete testadores e o `guardiao-da-ideia`, cada um com: nome do alvo, commit, caminho `work/alvos/main`, pasta da sprint, a lista das suas áreas do catálogo (tabela acima) e os achados abertos dessas áreas. Ao terminar cada um: `MA sprint marcar revisao:main:<agente>`.
- **Cada branch** (alvo que não é `main` e não tem `compartilha_com`): despache **um** `guardiao-da-ideia` com o diff `git -C work/espelho diff origin/main...<commit>` como foco e a instrução de revisar só o que a branch muda, em todas as áreas. Ao terminar: `MA sprint marcar revisao:<alvo>:guardiao-da-ideia`.
- **Cada alvo com `compartilha_com`** (mesma árvore de código de outro alvo): o código e a execução são os da origem, mas a documentação é revisada **por alvo**. Despache **um** `guardiao-da-ideia` focado em documentação: a documentação do alvo é lida com `git -C work/espelho show <commit>:<arquivo>` (liste com `git -C work/espelho ls-tree -r --name-only <commit>`) e conferida contra o código em `work/alvos/<origem>`. Ao terminar: `MA sprint marcar revisao:<alvo>:guardiao-da-ideia`.
- Os guardiões das branches e dos alvos com `compartilha_com` também vão juntos, na mesma mensagem (podem ir na mesma mensagem dos oito do `main`).
- **Sondas do agente não são da MAW.** Em todo despacho, avise: o código em `work/alvos/<alvo>` pode ter as sondas C++ do próprio agente — os arquivos em `Source/Tests/Sondas/` e as linhas que os acrescentam ao `.vcxproj` (injetadas antes do build Debug). Ignore-as: nada de achado, reverificação ou limitação sobre elas.
- Cada subagente grava seus achados em `achados-brutos/` via `MA achado registrar` (a CLI recusa item de catálogo inexistente, alvo ou commit errados e nome de arquivo repetido — o subagente corrige e registra de novo), suas reverificações em `reverificacoes-<agente>.json` (id → corrigido|persiste|nao_verificavel) e o que não conseguiu verificar em `limitacoes-<agente>.json` (lista de textos). O `MA sprint consolidar` junta sozinho todos os `reverificacoes-*.json`; o PDF lê todos os `limitacoes-*.json`.

**`--ate revisao`:** quando todos os subagentes da fase 3 terminarem e estiverem marcados, **pare aqui**. Responda só com os passos marcados nesta execução e o que ficou de fora. As fases seguintes rodam na próxima chamada.

## 4. Suíte e benchmark
(Com `--noturno`, pule: o script roda a suíte, a E2E, o serviço e a calibração e só chama você de novo quando tudo terminou.)
Quando a compilação terminar: `MA sprint suite` (em segundo plano). Ela só roda sobre o binário compilado nesta sprint, faz o backup do `%APPDATA%\MAW` só em volta da execução e o apaga depois da restauração verificada. Se a MAW do usuário estiver aberta, a suíte não roda (`recusado`) e isso vira `nao_testavel` no PDF; siga em frente.

## 5. Verificar
1. **Espere a suíte (fase 4) e TODOS os subagentes da fase 3 terminarem** antes de listar `achados-brutos/`. Quando os dois acabarem: `MA sprint marcar executar`.
2. Liste `achados-brutos/*.json`. Na retomada, pule os que já têm `vereditos/<mesmo nome>`.
3. Agrupe por arquivo e despache `advogado-do-diabo` (até 6 em paralelo, na mesma mensagem e em primeiro plano, cada um com até 8 arquivos e a pasta da sprint; se houver mais grupos, a próxima leva só depois que a anterior terminar). Cada um grava `<pasta da sprint>/vereditos/<nome exato do arquivo bruto>` — o mesmo nome, com a mesma extensão.
4. Quando todos terminarem: `MA sprint marcar verificar`. Achado de subagente sem veredito legível entra no PDF como provável e é declarado nas limitações; achado de build/suíte sem veredito mantém a confiança (a evidência é a saída da ferramenta).

## 6. Consolidar
`MA sprint consolidar`. Pode rodar de novo sem efeito colateral (parte sempre do histórico de antes da sprint).

## 7. Encerrar
`MA sprint encerrar` — confere que o ambiente foi restaurado e prova que a MAW ficou intocada. Rodar de novo devolve o que já foi registrado.

## 7b. Resultados na bancada
Pule se `bancada` já estiver concluído. Fica antes dos textos e do relatório para que uma falha aqui apareça no PDF.
1. `MA noturno bancada` — monta os resultados de `bancada.json` da pasta da sprint (gravado pelas fases E2E e serviço) em lotes de até 50. Se vier `desativada: true`, o usuário desligou a página: faça só o item 4, com `{"enviados": 0, "total": 0, "desativada": true}` (sem limitação). Se vier `ok: false` por outro motivo, a limitação já foi registrada: faça só o item 4, com `{"enviados": 0, "total": 0}`.
2. Para cada lote: **uma** chamada `ArtifactData` com `action: "batch"`, `url` = o `url` impresso e `writes` = o lote exatamente como veio (cada entrada é `{op: "set", collection: "resultados", doc_id: "<código>__windows__agente", data: {teste, plataforma: "windows", quem: "agente", estado, nota, em}}`). Não leia a página nem mude nada fora de `resultados/<código>__windows__agente`.
3. Se alguma chamada falhar (ferramenta indisponível, permissão negada, rede, página fora do ar): `MA noturno limitacao --origem bancada "resultados da bancada não enviados (<enviados> de <total>): <erro>"` e siga — os resultados continuam em `bancada.json` e no PDF.
4. `MA sprint marcar bancada --detalhe '{"enviados": <n>, "total": <total>}'`.

## 8. Textos
Despache `redator` com a pasta da sprint: ele lê `achados.json`, `alvos.json`, `builds/`, `suites/`, `benchmarks/`, `intocada.json`, `resultados.jsonl`, `erros_agente.json`, `derrubados.json`, `preflight.json` e `limitacoes-*.json`, e grava `textos.json`. Ao terminar: `MA sprint marcar textos`.

## 9. Relatório
1. `MA sprint relatorio` — gera o PDF (com `achados.json` anexado quando a consolidação rodou).
2. Commit e push no repositório **privado**, nunca no público: `git -C privado add relatorios historico catalogo`, depois `git -C privado commit -m "sprint NN: relatorio"` e `git -C privado push`. Se o push falhar, o commit fica local: diga isso na resposta final.

## 10. Resposta final
Diga ao usuário: caminho do PDF, veredito, contagem por severidade, regressões, se os resultados foram para a bancada e as principais limitações. Nada mais.
