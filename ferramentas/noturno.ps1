<#
.SYNOPSIS
  Sprint noturna do Agente MAW. O Agendador do Windows chama este script todo dia
  (registre com: .venv\Scripts\python -m maw_agent noturno instalar [--hora 22:00]).

.DESCRIPTION
  Ordem da noite (quem executa, cronometra e registra cada etapa e maw_agent\fase_noturno.py):
    1. sprint iniciar --nova, sprint preflight, sprint preparar (e sondas injetar)
    2. em paralelo: sprint compilar  e  claude -p "/sprint --retomar --ate revisao --noturno"
       (catalogo e revisao de codigo enquanto compila)
    3. depois do build: sprint suite, e2e, servico e calibrar (este so as segundas ou com -Calibrar)
    4. espera a revisao e chama claude -p "/sprint --retomar --noturno" (verificar, consolidar,
       encerrar, bancada, textos, relatorio, commit e push do privado). Se ele nao chegar ao PDF,
       o script consolida, encerra e gera o relatorio com o que houver.
  Subcomando que nao existe e pulado e vira limitacao declarada. Etapa com falha fica no log e a
  noite segue. Teclado e mouse reais (MAW_AGENTE_ENTRADA_REAL=1) so de noite, das 20:00 ate -Limite,
  e so com a sessao desbloqueada. Nada que abre a MAW comeca depois de -Limite, e a noite termina
  antes de -PrazoFinal (com uma hora guardada para o relatorio).
  Log completo e redigido: work\noturno\<aaaa-mm-dd>.log

.PARAMETER Ensaio
  So confere, sem mexer em nada: pre-voo (so leitura, sem sprint), subcomandos, claude --version,
  tarefa agendada e o plano da noite. Nao compila, nao abre a MAW e nao chama o claude -p.
  Log: work\noturno\<aaaa-mm-dd>-ensaio.log

.PARAMETER SoAlvo
  Compila e testa so este alvo (ex.: main).

.PARAMETER SemJulgamento
  Nao chama o claude -p: so as fases mecanicas e um relatorio parcial (sem commit nem push).

.PARAMETER Retomar
  Retoma a sprint em andamento em vez de comecar outra.

.PARAMETER EntradaReal
  auto (padrao: so de noite), sim ou nao.

.PARAMETER Limite
  Depois desta hora (padrao 07:00) nada que abre a MAW comeca.

.PARAMETER PrazoFinal
  A noite termina antes desta hora (padrao 11:00), com o relatorio.
#>
[CmdletBinding()]
param(
    [switch]$Ensaio,
    [switch]$Calibrar,
    [ValidatePattern('^[A-Za-z0-9._-]*$')][string]$SoAlvo = "",
    [switch]$SemJulgamento,
    [switch]$Retomar,
    [ValidatePattern('^\d{1,2}:\d{2}$')][string]$Limite = "07:00",
    [ValidateSet("auto", "sim", "nao")][string]$EntradaReal = "auto",
    [ValidatePattern('^\d{1,2}:\d{2}$')][string]$PrazoFinal = "11:00"
)

$ErrorActionPreference = "Continue"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $raiz

# O que todo processo da noite herda. MAW_AGENTE_ENTRADA_REAL e decidido pelo `noturno rodar`
# (auto = so de noite) e passado a cada etapa. CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0: o claude -p nao
# corta por inatividade um subagente em segundo plano (mesmo assim o /sprint --noturno despacha tudo em
# primeiro plano; comando Bash deixado em segundo plano morre logo depois da resposta final do claude -p).
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS = "0"
$env:MAW_AGENTE_NOTURNO = "1"

$py = Join-Path $raiz ".venv\Scripts\python.exe"
$pastaLogs = Join-Path $raiz "work\noturno"
$sufixo = ""
if ($Ensaio) { $sufixo = "-ensaio" }
$log = Join-Path $pastaLogs ((Get-Date -Format "yyyy-MM-dd") + $sufixo + ".log")

# So para quando o proprio Python nao consegue registrar (o resto do log e escrito por ele, redigido).
function Registrar([string]$texto) {
    New-Item -ItemType Directory -Force -Path $pastaLogs | Out-Null
    $linha = (Get-Date -Format "HH:mm:ss") + " [noturno.ps1] " + $texto + "`n"
    [System.IO.File]::AppendAllText($log, $linha, (New-Object System.Text.UTF8Encoding($false)))
}

$argumentos = @("-m", "maw_agent", "noturno", "rodar", "--limite", $Limite, "--entrada-real", $EntradaReal,
                "--prazo-final", $PrazoFinal)
if ($Ensaio) { $argumentos += "--ensaio" }
if ($Calibrar) { $argumentos += "--calibrar" }
if ($SoAlvo) { $argumentos += @("--so-alvo", $SoAlvo) }
if ($SemJulgamento) { $argumentos += "--sem-julgamento" }
if ($Retomar) { $argumentos += "--retomar" }

if (-not (Test-Path -LiteralPath $py)) {
    Registrar ("Python do agente nao encontrado (" + $py + "): a noite nao rodou")
    exit 2
}
try {
    & $py @argumentos
    $codigo = $LASTEXITCODE
} catch {
    Registrar ("o Python do agente nao comecou: " + $_.Exception.Message)
    exit 3
}
if ($codigo -ne 0) {
    Registrar ("noturno rodar terminou com codigo " + $codigo + " (o motivo esta nas linhas acima)")
}
exit $codigo
