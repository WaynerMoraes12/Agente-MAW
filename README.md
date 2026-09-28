# Roadie

**Roadie** é o agente de testes da **MAW** (uma DAW para Windows). Como o roadie de
uma banda, ele testa cada cabo, microfone e instrumento antes do show, mas nunca
sobe ao palco para tocar. A cada sprint ele testa
**tudo, sempre, de ponta a ponta**, no `main` e em cada branch aberta, e entrega
um **PDF** com os achados: um resumo para quem decide o merge e fichas técnicas
exatas para quem vai corrigir, seja pessoa ou agente de IA.

O agente **nunca altera a MAW**. Ele lê, compila numa cópia própria e reporta.

## Como funciona

Um comando do Claude Code, `/sprint`, orquestra uma CLI Python determinística
(`python -m maw_agent`) e um conjunto de subagentes especializados.

```
iniciar → pré-voo → preparar → catalogar → compilar → revisão de código
       → suíte e benchmark → verificação adversarial → consolidar
       → encerrar (prova de MAW intocada) → textos → relatório (PDF)
```

- **Catálogo exaustivo.** A fonte da verdade da cobertura é um catálogo de
  funcionalidades. O PDF mostra 100% do catálogo × todos os alvos, e nenhuma
  célula fica vazia: o que não pôde ser testado aparece como *não testável*,
  com o motivo.
- **Alvos.** `origin/main`, as branches do GitHub ainda não mescladas e os
  commits de clones locais que ainda não foram enviados. Dois alvos com a
  mesma árvore de código compartilham a execução.
- **Evidência ou não é achado.** Todo achado tem passos, esperado × obtido,
  evidência, causa provável (marcada como hipótese) e critério de aceite
  verificável. Um subagente advogado-do-diabo tenta derrubar cada um antes do
  PDF.
- **Histórico entre sprints.** Os IDs são estáveis (`MAW-0001`). Cada achado é
  *novo*, *aberto*, *corrigido* (só quando o critério de aceite passa, nunca
  por ausência), *regressão* ou *não verificável*.
- **PDF para dois públicos.** Capa com veredito e semáforo por alvo, fichas
  técnicas, aderência aos princípios do projeto, saúde técnica, matriz de
  cobertura e limitações declaradas. O `achados.json` vai anexado dentro do
  próprio PDF.

## Garantias

- **A MAW não é alterada.** O agente usa um clone próprio com push
  desativado. Nas pastas do usuário, o git só roda comandos de leitura com
  `--no-optional-locks`. A única porta de escrita do código (`sandbox.py`)
  recusa esses caminhos. A cada sprint o PDF traz a prova "antes × depois".
- **Nada sigiloso no repositório público.** O catálogo, os princípios, o
  histórico e os PDFs ficam num repositório **privado** separado, clonado em
  `privado/`. Um hook de pre-commit bloqueia segredos, caminhos de resultado e
  uma lista de termos internos mantida no privado. Sem essa lista, o hook
  recusa o commit.
- **Configurações do usuário preservadas.** O `%APPDATA%\MAW` recebe backup
  só em volta da execução da MAW, fora da árvore do repositório. A restauração
  é verificada, e o backup é apagado depois. Se uma execução cair no meio, a
  próxima restaura o estado original, e nunca com a MAW aberta.

## Requisitos

- Windows 10/11, com Visual Studio 2022 Build Tools (MSBuild) e Microsoft Edge.
- Python 3.13 (recomendado: [uv](https://docs.astral.sh/uv/)).
- Claude Code, para o comando `/sprint` e os subagentes.
- Acesso ao repositório da MAW e ao repositório privado do agente.

## Instalação

```powershell
uv sync                      # cria .venv (Python 3.13) com as dependências do pyproject.toml
git clone <repositório privado> privado
.venv\Scripts\python.exe -m maw_agent instalar-hooks
.venv\Scripts\python.exe -m pytest
```

## Uso

No Claude Code, na raiz do repositório:

```
/sprint            # roda ou retoma a sprint em andamento
/sprint --nova     # começa uma sprint nova
```

O PDF fica em `privado/relatorios/sprint-NN/MAW-Sprint-NN.pdf`.

## Estrutura

```
maw_agent/            CLI e módulos (alvos, build, suíte, achados, catálogo,
                      estado, pré-voo, sandbox, relatório em PDF)
.claude/commands/     /sprint
.claude/agents/       testadores por área, guardião da ideia,
                      advogado-do-diabo, catalogador, redator
ferramentas/hooks/    pre-commit
docs/superpowers/     spec de design e planos de implementação
tests/                testes do próprio agente
```

## Estado

O **M1** (esqueleto confiável) está pronto: compilação, suíte existente,
benchmark, revisão de código por especialistas, verificação adversarial,
histórico e PDF. Os próximos marcos acrescentam E2E na GUI real (M2), áudio e
MIDI ao vivo (M3), o serviço de IA (M4), sondas C++ (M5) e calibração (M6).
Veja a [spec](docs/superpowers/specs/2026-09-27-agente-maw-design.md).
