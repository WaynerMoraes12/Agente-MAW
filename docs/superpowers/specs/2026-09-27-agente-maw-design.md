# Roadie (agente de testes da MAW): design

**Data:** 2026-09-27
**Autor:** Claude (com Wayner)
**Status:** aguardando revisão

Este documento descreve o agente e fica no repositório **público**. O
conhecimento interno da MAW necessário para operá-lo fica no apêndice privado:
inventário de funcionalidades, os princípios com as citações de origem,
lacunas da suíte atual, caminhos fixos e primeiros candidatos a bug. O
apêndice está em `privado/docs/2026-09-27-apendice-maw.md`.

---

## 1. Objetivo

Um agente de testes que, **ao fim de cada sprint** da MAW:

1. testa **todas** as funcionalidades da MAW, **sempre** e **de ponta a
   ponta**, sem amostragem e sem se limitar ao que mudou;
2. faz isso no **main** e em **cada branch aberta**;
3. encontra **erros, bugs, violações da ideia da MAW, afirmações falsas da
   documentação, lacunas de teste e melhorias**;
4. julga tudo **pela ideia da MAW** (seus princípios de projeto) e trabalha
   do mesmo jeito que ela: com evidência, sem falhar em silêncio e declarando
   os próprios limites;
5. gera um **PDF** por sprint, legível por humanos (resumo executivo) e por um
   agente de IA de correção (fichas técnicas executáveis);
6. **nunca altera a MAW.**

### Critérios de sucesso

- A matriz de cobertura do PDF lista **100% do catálogo × todos os alvos**.
  Cada célula é *passou*, *falhou (ID)*, *não testável (motivo)* ou *n/a*
  (a funcionalidade não existe naquele alvo). Nenhuma célula fica vazia.
- Todo achado no PDF tem evidência e critério de aceite verificável, e passou
  pela verificação adversarial.
- Um achado corrigido entre duas sprints aparece como **corrigido** porque o
  cenário de reprodução passou, e não porque sumiu.
- Na **calibração** (seção 14), o agente encontra defeitos reinjetados de
  propósito numa cópia da MAW.
- O PDF traz "MAW intocada: verificado", com a prova.

### Não objetivos

- Corrigir bugs. O agente reporta; quem corrige é quem recebe o PDF.
- Alterar, commitar ou fazer push na MAW ou nos clones dela.
- Medir desempenho em outras máquinas ou certificar hardware de áudio.
- Julgar gosto musical ou decisões de produto que não violem um princípio.

## 2. Vocabulário

| Termo | Significado |
|---|---|
| **alvo** | um estado da MAW a testar: o `main` ou uma branch aberta, sempre num commit exato |
| **sprint** | um ciclo de desenvolvimento da MAW; ao final dele o usuário roda `/sprint` |
| **catálogo** | a lista exaustiva do que a MAW faz; é a fonte da verdade da cobertura |
| **cenário** | um teste ponta a ponta de um item do catálogo |
| **sonda** | um teste C++ novo, escrito pelo agente e compilado contra o código do alvo |
| **achado** | um problema ou uma melhoria, com ficha, evidência e ID estável |
| **evidência** | screenshot, captura de áudio, medição, log, `.maw` antes e depois, `arquivo:linha` |

## 3. Garantia de não alteração da MAW

A garantia é estrutural; não depende de disciplina.

- O agente mantém **seu próprio clone** da MAW em `work/espelho/`, baixado do
  GitHub. O `pushurl` desse clone é inválido (`nao-faca-push`), então um push
  acidental falha.
- Worktrees, builds, `fetch` e qualquer edição (por exemplo, acrescentar as
  sondas ao projeto de build) acontecem **só** em `work/`.
- As pastas da MAW do usuário (`C:\Users\User\MAW*`) são **apenas lidas**.
  Commits de um clone local que ainda não estão no GitHub são puxados **a
  partir** do clone, o que só lê a origem. Dentro delas só rodam comandos git
  de leitura (`rev-parse`, `status`, `diff`), sempre com
  `--no-optional-locks`, para que nem o índice seja atualizado.
- Alterações **não commitadas** num clone local não são testadas; o PDF avisa
  que elas existem.
- `maw_agent/sandbox.py` expõe a única função de escrita em disco usada pelo
  agente, e ela **recusa** qualquer caminho sob as pastas da MAW do usuário.
  Esse comportamento tem teste próprio.
- **Prova:** no início e no fim da sprint, o agente registra de cada pasta da
  MAW do usuário o HEAD, o `git status` e um hash do conteúdo rastreado. O PDF
  diz "MAW intocada: verificado" ou lista o que mudou.
- O ambiente que o agente toca sempre volta ao estado original, e isso também
  vai para o PDF:
  - `%APPDATA%\MAW`: backup antes, restauração depois;
  - o caminho fixo onde a MAW procura o serviço de IA: criado e removido, ou
    restaurado se já existia.

## 4. Repositórios e segredos

| Onde | O que vai |
|---|---|
| **Público** `WaynerMoraes12/Roadie` | `CLAUDE.md` genérico, `/sprint`, subagentes, `maw_agent/` (ferramentas), gerador de PDF, testes do agente, esta spec |
| **Privado** `WaynerMoraes12/Roadie-privado`, clonado em `privado/` | catálogo, cenários, sondas, princípios com citações, fixtures específicas, apêndice, registro de achados, PDFs |
| **Nenhum repositório** | backups do `%APPDATA%\MAW`, chave do Gemini, evidências brutas, `work/` |

A MAW é privada, e por isso o conhecimento interno dela não vai para o
repositório público.

**Defesa em camadas:**

1. Um `.gitignore` em cada repositório.
2. Um hook de **pre-commit** nos dois repositórios que bloqueia:
   - chaves (`AIza[0-9A-Za-z_-]{35}` e padrões genéricos de token);
   - PDFs e qualquer coisa sob `relatorios/`, `evidencias/`, `work/`,
     `privado/` (no público);
   - no público, também nomes de classes e arquivos internos da MAW, a partir
     de uma lista de termos proibidos mantida no repositório **privado**, para
     que a própria lista não vaze. Sem a lista presente, o hook **recusa** o
     commit em vez de deixá-lo passar sem essa checagem.
3. Um **depurador de evidências** que troca por `[REDACTED]` qualquer chave
   encontrada em logs e saídas antes de gravá-los.
4. A chave do Gemini é lida do lugar onde a MAW já a guarda, só em memória, e
   nunca é escrita pelo agente.

## 5. Arquitetura

### 5.1 Fluxo do `/sprint`

```
0. Pré-voo     MAW fechada? VB-Cable presente? Python 3.10/ffmpeg? disco livre?
               DPI/resolução? Avisa que a fase de GUI assume mouse e teclado.
               Cada pré-requisito que faltar vira limitação declarada; nunca é pulado em silêncio.
1. Preparar    atualiza work/espelho, descobre alvos (main + branches não mescladas),
               cria worktrees, registra a prova "antes" (seção 3), faz backup do %APPDATA%\MAW
2. Compilar    dois builds por alvo, em paralelo quando a máquina permite:
               Release (E2E e benchmark) e Debug já com as sondas (suíte + sondas)
               build quebrado = achado crítico; o alvo segue só com revisão de código
3. Catalogar   o catalogador compara cada alvo com o catálogo: funcionalidade nova
               entra com cenário escrito na mesma sprint; removida vira pendência
4. Executar    para CADA alvo, o catálogo inteiro:
               4a suíte existente (--run-tests, Debug) + benchmark (--benchmark, Release)
               4b sondas C++ (binário próprio, Debug)
               4c E2E na GUI real (Release), em série
               4d serviço de IA (HTTP direto + fluxo pela interface)
               4e revisão de código pelos subagentes especialistas + guardião da ideia
5. Verificar   o advogado-do-diabo tenta derrubar cada achado
6. Consolidar  deduplica entre alvos, atribui IDs estáveis, compara com a sprint anterior
7. Encerrar    restaura o ambiente, registra a prova "depois", compara as duas
8. Relatório   gera o PDF (que já traz a prova de intocada), com achados.json anexado;
               a sprint só conta como concluída quando o PDF existe
```

As fases 2 e 4e rodam em paralelo; a 4c roda em série, porque a GUI tem um
mouse só.

**Descoberta de alvos:**
- `origin/main`;
- cada branch do GitHub que não é ancestral do `origin/main`;
- o HEAD de cada clone local que não está mesclado (o mais novo vence quando
  local e GitHub têm a mesma branch; os dois entram quando divergem).

**Mesma árvore, mesma execução:** dois alvos com a mesma árvore nos caminhos
de código (por exemplo, uma branch que só muda documentação) compartilham a
execução, e o PDF declara isso na célula. É o mesmo binário, então rodar de
novo não traria informação nova. A revisão da documentação continua sendo
feita por alvo. Cada fase grava seu progresso em `estado.json`, e `/sprint
--retomar` continua de onde parou (reboot, queda de energia, interrupção).

### 5.2 Divisão entre ferramentas e subagentes

- **Ferramentas Python (determinísticas)** fazem o que precisa ser exato:
  compilar, dirigir a GUI, medir áudio, comparar arquivos, registrar achados e
  montar o PDF. Rodam como `python -m maw_agent <fase> [--alvo X]`, sempre
  com saída estruturada (JSON).
- **Subagentes (julgamento)**:
  - leem o código atrás de bugs;
  - exploram casos de borda na GUI além do roteiro;
  - avaliam screenshots;
  - julgam a aderência aos princípios;
  - escrevem cenários de funcionalidades novas;
  - tentam derrubar achados;
  - redigem o texto do PDF.
- **Quem decide o que é testado é o catálogo, nunca um subagente.** A
  exploração livre acrescenta profundidade, mas não substitui item nenhum.

## 6. Componentes

### 6.1 Repositório público

```
Agente MAW/
├─ CLAUDE.md                  missão, regras de evidência, "nunca alterar a MAW",
│                             onde está o conhecimento privado
├─ .claude/
│  ├─ commands/sprint.md      orquestra as fases 0–8; aceita --retomar e --alvo
│  └─ agents/
│     ├─ testador-motor.md       transporte, metrônomo, gravação, latência, taxa de amostragem
│     ├─ testador-edicao.md      timeline, clips, seleção, snap, marcadores, undo
│     ├─ testador-midi.md        MIDI ao vivo, piano roll, sintetizador, arquivos .mid
│     ├─ testador-efeitos.md     efeitos nativos, VST3, rack, painéis
│     ├─ testador-projeto.md     arquivo de projeto, autosave, recentes, modelos, pasta portátil, exportação
│     ├─ testador-interface.md   menus, atalhos, foco, diálogos, paleta, UX visual
│     ├─ testador-ia.md          serviço Python, separação, transcrição, conselheiro, Gemini, Smart Mix
│     ├─ guardiao-da-ideia.md    princípios e afirmações da documentação × código e comportamento
│     ├─ catalogador.md          mantém o catálogo e escreve cenários novos
│     └─ advogado-do-diabo.md    verificação adversarial
├─ maw_agent/
│  ├─ __main__.py             CLI das fases
│  ├─ alvos.py                espelho, descoberta de branches, worktrees, prova antes/depois
│  ├─ build.py                localiza o MSBuild (vswhere), compila Release/Debug, coleta warnings
│  ├─ suite.py                roda --run-tests/--benchmark esperando o processo e lê os relatórios
│  ├─ sondas.py               injeta as sondas numa cópia do projeto de build do alvo e compila
│  ├─ app.py                  driver da GUI: UI Automation, teclado, mouse, geometria, DPI
│  ├─ audio.py                injeção e captura (VB-Cable, loopback WASAPI) e medições
│  ├─ midi.py                 porta Loopback do Windows MIDI Services
│  ├─ servico.py              Python 3.10 via uv, ffmpeg portátil, sobe o serviço, sondas HTTP
│  ├─ sandbox.py              escrita protegida, backup e restauração do ambiente
│  ├─ saude.py                crash, travamento, jassert (saída de depuração), memória e handles
│  ├─ achados.py              ficha, impressão digital, estados, deduplicação, histórico
│  ├─ redacao.py              depurador de segredos
│  └─ relatorio/              modelo HTML/CSS + renderização em PDF + anexo JSON
├─ ferramentas/hooks/pre-commit
├─ tests/                     testes do próprio agente (seção 14)
└─ docs/superpowers/          specs e planos
```

### 6.2 Repositório privado (`privado/`)

```
privado/
├─ catalogo/funcionalidades.yaml
├─ catalogo/principios.yaml
├─ catalogo/termos-proibidos.txt     usado pelo hook do repositório público
├─ cenarios/<area>/*.py              E2E (pytest)
├─ sondas/*.cpp                      juce::UnitTest, categoria "MAW", nomes "SONDA ..."
├─ fixtures/                         geradores de áudio e de projetos de teste
├─ docs/2026-09-27-apendice-maw.md
├─ relatorios/sprint-NN/
│  ├─ MAW-Sprint-NN.pdf
│  ├─ achados.json
│  ├─ evidencias/                    só local (ignorado)
│  └─ estado.json                    só local (ignorado)
└─ historico/achados.json            registro acumulado entre sprints (IDs estáveis)
```

## 7. Catálogo

`funcionalidades.yaml`, um item por funcionalidade observável:

```yaml
- id: projeto/relink-pasta-movida
  area: projeto
  titulo: Projeto cujo áudio mudou de lugar oferece religar
  descricao: >
    Abrir um projeto cujos arquivos de áudio sumiram contorna os clips em
    vermelho, oferece localizar, e religa todos os faltantes da mesma pasta.
  origem: [README principal, secao "Core Architecture"]
  verificacao: [e2e, revisao]
  cenarios: [projeto/test_relink.py::test_pasta_movida_religa_todos]
  requisitos: []            # ex.: [vb-cable], [python310], [gemini]
  presente_em: auto         # calculado por alvo pelo catalogador
```

- **Granularidade:** cada item de menu, atalho, botão, efeito e parâmetro,
  opção de exportação, campo do arquivo de projeto, diálogo, rota do serviço e
  princípio é um item próprio.
- **`principios.yaml`:** um item por princípio, com a forma de verificá-lo
  (qual cenário, qual sonda, qual regra de revisão).
- **Atualização por sprint:** o catalogador lê os READMEs, os menus e
  atalhos declarados no código e as rotas do serviço de cada alvo.
  - Funcionalidade nova entra no catálogo **com cenário escrito na mesma
    sprint**.
  - Se o cenário não puder ser escrito, a célula fica *não testável: cenário
    pendente* e isso aparece nas limitações. Não há item sem cenário que não
    seja declarado.
  - Funcionalidade removida vira pendência para o usuário confirmar.
- **Funcionalidade só de uma branch** é testada só nos alvos onde existe; nos
  outros, a célula é *n/a*, distinta de *não testável*.

## 8. Cenários E2E

Cada cenário segue o ciclo **estado conhecido → gesto real → observação
independente → evidência**.

- **Estado conhecido:**
  - projetos de teste gerados pelas fixtures, com áudios de propriedades
    mensuráveis (senos com frequência e nível conhecidos, trem de cliques num
    BPM conhecido, acordes, trecho de fala, música com voz e bateria);
  - antes de cada grupo, a MAW é fechada, o `%APPDATA%\MAW` de teste é
    restaurado e o projeto é aberto pelo caminho do usuário (o seletor nativo
    do Windows);
  - a geometria do que é só desenhado (clips, notas, marcadores, régua) é
    **calculada** a partir de zoom, altura de trilha e escala de DPI.
- **Gesto real:**
  - UI Automation para o que tem nome (botões, itens de menu, caixas de
    diálogo, janelas);
  - teclado real para os atalhos;
  - mouse em coordenadas calculadas para o restante.
- **Observação independente:** o cenário nunca confia só no que a MAW diz de
  si mesma.
  - **Áudio de saída:** capturado e medido em Python (frequência, nível, fase
    do metrônomo, mute, solo, pan, latência).
  - **Arquivos:** projeto salvo relido como XML e comparado campo a campo;
    exportações medidas (duração, pico, loudness e true peak por um medidor
    BS.1770 **independente**, dither, formato); gravações, autosave,
    configurações, recentes e modelos.
  - **Tela:** screenshot a cada passo. Onde o esperado é objetivo, há checagem
    de pixel (por exemplo, "toda cor pertence à paleta"). Os subagentes
    avaliam o que é visual ou de UX.
  - **Processo:** crash, janela que não responde, crescimento de memória e
    handles. O E2E usa o Release, que é o que o usuário roda; `jassert` é
    capturado nas execuções Debug (suíte e sondas).
- **Evidência:** tudo vai para `evidencias/<alvo>/<cenario>/`, e o PDF recebe
  o essencial.

**Dispositivos de áudio.** Dois modos, conforme o que o pré-voo encontra
(verificações `entrada_injetavel` e `loopback`):

**Placa interna (modo padrão — decisão de 28/09, sem VB-Cable no ambiente).** A
MAW toca pela placa de som interna do Windows e o agente observa pelo loopback
WASAPI da saída padrão (`pyaudiowpatch`). Sem cabo virtual não há como injetar
um sinal conhecido na entrada da MAW: gravação com sinal conhecido, afinador e
qualquer cenário que dependa de um sinal controlado na entrada viram *pulei*/
*não testável* com o motivo. O que depende só da saída — nível, frequência,
mute, solo, pan, latência, fades, automação, metrônomo — continua medido pelo
loopback.

| Grupo | Saída da MAW | Entrada da MAW | O agente |
|---|---|---|---|
| reprodução | placa interna | — | captura por loopback |
| monitoração | placa interna | — | injeta e captura pelo mesmo loopback |
| gravação com sinal conhecido | — | — | *pulei*: sem cabo virtual, não há como injetar |

**Com VB-Cable (se `entrada_injetavel` estiver ok).** O cabo gratuito é um cabo
só, então a configuração de dispositivos da MAW muda **por grupo de cenários**:

| Grupo | Saída da MAW | Entrada da MAW | O agente |
|---|---|---|---|
| reprodução | cabo | — | captura do cabo |
| gravação | placa interna (volume do sistema a 0) | cabo | injeta no cabo |
| monitoração | placa interna, capturada por loopback | cabo | injeta no cabo e captura o loopback |

Um cenário que se mostre impossível com um cabo só vira *não testável* com
motivo, e o PDF sugere o VB-Cable A+B.

**MIDI ao vivo:** notas, pitch bend, mod wheel e sustain enviados pela porta
Loopback do Windows MIDI Services. Se a MAW não enxergar a porta, os itens de
MIDI ao vivo viram *não testável* com motivo.

**Instabilidade:** um cenário que falha é repetido uma vez.

- Falha nas duas vezes: vale o resultado.
- Falha em uma das duas: fica **instável**, e o advogado-do-diabo investiga se
  a causa está na MAW (vira achado) ou no agente (vira erro do agente, listado
  nas limitações).

## 9. Sondas C++

- São testes `juce::UnitTest` escritos pelo agente para as lacunas da suíte
  existente (lista no apêndice privado), na categoria que o `--run-tests` já
  roda e com nomes começando por `SONDA`.
- `sondas.py` acrescenta os `.cpp` à cópia do projeto de build do alvo dentro
  de `work/`, e o build Debug do alvo já sai com elas. A suíte original e as
  sondas rodam juntas, e o relatório separa os resultados pelo prefixo. As
  sondas só acrescentam classes de teste, então o resultado da suíte original
  não muda por causa delas.
- Se o Debug não compilar **por causa de uma sonda**, ela é retirada, o Debug
  é recompilado sem ela e a retirada vai para as limitações. Assim a suíte
  original sempre roda.
- Uma sonda que não compila contra um alvo (API mudou) é um erro do agente, e
  não da MAW. O catalogador a atualiza na mesma sprint; se não conseguir, ela
  fica declarada nas limitações.

## 10. Serviço de IA

- **Python 3.10** instalado com `uv` em `work/py310/` (não exige
  administrador); dependências do `requirements.txt` do alvo; **ffmpeg**
  portátil em `work/ferramentas/`, posto no PATH só do processo do serviço.
- **HTTP direto:** cada rota com entrada válida, entrada inválida, campo
  faltando e arquivo inexistente. O que se verifica é o formato da resposta,
  os códigos HTTP e o princípio de nunca engolir o erro real.
- **Pela interface:** o agente coloca o serviço do alvo no caminho fixo onde a
  MAW o procura, só durante o teste, e dirige o fluxo completo (stems voltam
  como trilhas, transcrição vira marcadores, conselheiro responde).
- **Caches:** modelos de separação baixados na primeira vez e o modelo de
  transcrição (~3 GB) ficam em `work/`, reaproveitados entre sprints.
- **Gemini:** usa a chave que a MAW já guarda, uma chamada real por alvo pelo
  fluxo da interface e uma pela rota direta. A chave nunca é gravada, e as
  saídas passam pelo depurador de segredos.

## 11. Achados

### 11.1 Tipos e severidade

| Tipo | Exemplo |
|---|---|
| **erro** | crash, travamento, `jassert`, build quebrado, teste da suíte falhando |
| **bug** | comportamento errado |
| **violação da ideia** | quebra de um princípio (falha silenciosa, dois níveis de undo num gesto, cor fora da paleta) |
| **afirmação falsa** | a documentação diz algo que o código ou o comportamento não fazem |
| **lacuna de teste** | área sem cobertura na suíte da MAW |
| **melhoria** | sugestão coerente com a ideia; o guardião descarta as que contradizem um princípio |

| Severidade | Critério |
|---|---|
| **crítica** | perda de trabalho ou de áudio, crash, arquivo corrompido, build quebrado |
| **alta** | a funcionalidade não funciona, ou falha em silêncio |
| **média** | funciona com defeito, ou existe contorno |
| **baixa** | cosmético ou texto |

Melhorias recebem **prioridade** (alta, média, baixa) em vez de severidade.

### 11.2 Ficha

Campos obrigatórios (em JSON no `achados.json` e renderizados no PDF):

- `id`, `titulo`, `tipo`, `severidade` ou `prioridade`
- `alvos`: lista de `{alvo, commit}`, e `introduzido_por` quando só existe numa branch
- `item_catalogo`, `principio` (se houver)
- `passos`: numerados e exatos
- `esperado`, `obtido`
- `evidencias`: referências aos arquivos locais + o que vai embutido no PDF
- `causa_provavel`: `arquivo:linha` + texto, sempre marcada como hipótese
- `sugestao`: dentro da ideia da MAW
- `criterio_aceite`: verificável, normalmente "o cenário X passa" ou "a sonda Y passa"
- `confianca`: `confirmado` (reproduzido) ou `provavel` (evidente no código, sem reprodução dinâmica)
- `estado`: `novo`, `aberto`, `corrigido`, `regressao`, `nao_verificavel`
- `historico`: sprints em que apareceu

### 11.3 Verificação adversarial

O advogado-do-diabo recebe cada achado sem o raciocínio de quem o encontrou.

- Para E2E, reexecuta o cenário.
- Para revisão de código, procura a proteção ou a condição que tornaria aquilo
  um não-bug e confere a documentação.

Veredito:
- **confirmado**: entra no PDF;
- **provável**: entra, marcado assim;
- **derrubado**: sai do PDF, mas o total de derrubados e um resumo aparecem
  nas limitações.

### 11.4 Deduplicação e histórico

- **Impressão digital:** item do catálogo + tipo + assinatura normalizada
  (asserção que falhou, ou arquivo e função da causa). Ela mantém o ID
  (`MAW-0001`, sequencial, nunca reaproveitado) entre alvos e entre sprints.
- A mesma impressão em vários alvos vira **um** achado com a lista de alvos.
  Se ela só aparece numa branch, o achado ganha `introduzido_por: <branch>`.
- **Transição de estados** a cada sprint, comparando com
  `historico/achados.json`:
  - não existia → **novo**;
  - existia e reproduz → **aberto**;
  - existia e o critério de aceite agora passa → **corrigido**;
  - estava corrigido e reproduz de novo → **regressão**;
  - o alvo ou o requisito não pôde ser testado → **não verificável**, com o
    estado anterior mantido.

## 12. PDF

`privado/relatorios/sprint-NN/MAW-Sprint-NN.pdf`, em português.

**Parte 1: para humanos**

1. Capa: sprint, data, alvos com commit, duração, veredito geral.
2. Resumo executivo:
   - semáforo por alvo (*pronto para merge*, *com ressalvas*, *bloqueado*);
   - achados por severidade;
   - novos, corrigidos, abertos e regressões;
   - os 5 mais graves;
   - recomendação de merge por branch;
   - "MAW intocada: verificado".
3. Comparação com a sprint anterior.
4. Uma seção por branch: o que muda, os achados que ela introduziu, a
   recomendação.

**Parte 2: técnica**

5. Instruções para o agente de correção:
   - reproduzir antes de corrigir;
   - respeitar os princípios;
   - nunca alterar um teste para ele passar;
   - um achado por commit;
   - o critério de aceite é o cenário indicado passar.
6. Fichas dos achados, por severidade, com evidência embutida.
7. Melhorias, por prioridade, cada uma dizendo a qual princípio ou objetivo
   serve.
8. Aderência à ideia: princípios × alvos, e cada afirmação da documentação
   classificada como *verdadeira*, *falsa* ou *não verificável*, com a prova.
9. Saúde técnica: build, suíte, benchmark (comparado com a sprint anterior e
   com a tabela publicada), crashes, travamentos, `jassert`, memória.

**Parte 3: prova**

10. Matriz de cobertura (paisagem): 100% do catálogo × alvos.
11. Limitações da sprint:
    - o que não pôde ser testado e por quê;
    - achados prováveis;
    - derrubados;
    - erros do próprio agente.
12. Anexo de ambiente e método: versões, dispositivos, DPI, build do Windows,
    duração por fase, índice de evidências.

**Tecnologia:** modelo HTML/CSS (Jinja2) renderizado em PDF pelo **Edge
instalado no Windows**, via Playwright com `channel="msedge"` (nenhum
navegador é baixado). Cabeçalho e rodapé com numeração de páginas. O
`achados.json` é anexado ao PDF como arquivo embutido (pypdf).

**Identidade visual:** capa escura com a marca da MAW (lida do espelho em
tempo de execução, nunca copiada para o repositório público) e páginas
internas claras com o acento roxo da paleta.

## 13. Tratamento de erros

- **O agente distingue o próprio erro de um erro da MAW.** Controle não
  encontrado, sonda que não compila e ferramenta que falha são investigados
  antes de virar achado. Se a causa estiver no agente, o problema vai para
  "erros do agente" nas limitações, nunca para as fichas.
- **Pré-requisito ausente** vira *não testável* com motivo em cada célula
  afetada. Nada é pulado em silêncio.
- **Crash ou travamento da MAW durante um cenário:**
  - o cenário é registrado (achado de erro, com o dump ou o estado da janela);
  - a MAW é reiniciada e o ambiente restaurado;
  - a sprint segue para o próximo cenário.
- **Interrupção** (reboot, energia, Ctrl+C): `estado.json` permite retomar;
  o ambiente é restaurado na retomada antes de qualquer coisa.
- **Prova de intocada divergente:** o PDF diz exatamente o que mudou, em
  destaque na capa.

## 14. Testes do próprio agente

- **Unidade (pytest):**
  - medições de áudio contra sinais sintéticos com resposta conhecida;
  - impressão digital e máquina de estados dos achados;
  - deduplicação;
  - depurador de segredos;
  - a escrita protegida recusa as pastas da MAW;
  - backup e restauração do ambiente de ponta a ponta;
  - o PDF é gerado a partir de dados de exemplo e o anexo é recuperável;
  - o hook de pre-commit bloqueia os casos da seção 4.
- **Calibração** (antes da primeira sprint real, e sempre que o agente mudar
  muito): numa worktree do espelho, o agente reinjeta defeitos conhecidos da
  história da MAW (os do apêndice) e defeitos plantados nas lacunas (por
  exemplo, uma falha silenciosa num diálogo). Depois roda o fluxo e precisa
  encontrá-los. O resultado da calibração vai para o PDF daquela execução.

## 15. Pré-requisitos do ambiente

A cargo do usuário, uma vez:

- instalar o **VB-Cable** (exige administrador);
- deixar o PC livre durante a fase de GUI e manter a MAW fechada;
- ativar o "Não perturbe" do Windows durante a sprint, para que notificações
  não roubem o foco. O pré-voo avisa se estiver desligado.

A cargo do agente:

- Python 3.10 (uv);
- ffmpeg portátil;
- dependências Python do agente;
- Playwright sem download de navegador;
- o espelho da MAW.

## 16. Riscos conhecidos

| Risco | Mitigação |
|---|---|
| O que é só desenhado não aparece na UI Automation | geometria calculada + screenshots + checagem de pixel |
| Escala de DPI e resolução | geometria multiplicada pela escala lida do sistema; o pré-voo registra a resolução |
| Duração: main + ~12 branches × catálogo inteiro deve levar várias horas | execução em segundo plano, retomável, com a duração por fase no anexo; nada é cortado para caber no tempo |
| Um cabo de áudio só | grupos de dispositivos; *não testável* declarado quando não bastar |
| Foco roubado por outra janela | o driver confere o foco antes de cada gesto; perda de foco reinicia o passo e é registrada |
| Cenário frágil gerando falso positivo | repetição + advogado-do-diabo + classificação *instável* |
| Custo de Gemini e tempo de download de modelos | chamadas limitadas por alvo; caches em `work/` |

## 17. Entrega incremental

Cada marco termina com um PDF real, cada vez mais completo:

1. **M1: esqueleto confiável.**
   - dois repositórios, hook, escrita protegida e prova de intocada;
   - espelho e alvos;
   - build;
   - suíte e benchmark;
   - registro de achados;
   - revisão de código pelos subagentes;
   - PDF completo com cobertura parcial declarada.
2. **M2: catálogo e GUI.** Catálogo inteiro, driver de GUI, cenários E2E de
   interface, edição, projeto, efeitos e exportação.
3. **M3: áudio e MIDI ao vivo.** VB-Cable, loopback, Windows MIDI Services;
   cenários de motor, gravação e MIDI.
4. **M4: serviço de IA.** Python 3.10, ffmpeg, rotas e fluxos pela interface,
   Gemini.
5. **M5: sondas C++.** Uma sonda para cada lacuna do apêndice.
6. **M6: calibração e Sprint 1.** Calibração, primeira sprint real completa
   (main + todas as branches) e publicação dos repositórios no GitHub.
