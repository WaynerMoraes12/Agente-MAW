# Sintetiza um texto (Base64 UTF-8, para não depender da code page do console) num WAV via
# SAPI (System.Speech), preferindo uma voz cuja cultura comece com -VozPrefixo. Imprime
# "NomeDaVozUsada|CulturaDaVozUsada" no stdout para o chamador registrar qual voz saiu.
param(
    [Parameter(Mandatory=$true)][string]$TextoBase64,
    [Parameter(Mandatory=$true)][string]$Caminho,
    [string]$VozPrefixo = "pt-BR"
)
Add-Type -AssemblyName System.Speech
$sintetizador = New-Object System.Speech.Synthesis.SpeechSynthesizer
$texto = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($TextoBase64))

$escolhida = $null
foreach ($voz in $sintetizador.GetInstalledVoices()) {
    if ($voz.Enabled -and $voz.VoiceInfo.Culture.Name -like "$VozPrefixo*") {
        $escolhida = $voz
        break
    }
}
if ($escolhida -ne $null) {
    $sintetizador.SelectVoice($escolhida.VoiceInfo.Name)
}
$nomeUsado = $sintetizador.Voice.Name
$culturaUsada = $sintetizador.Voice.Culture.Name

$sintetizador.SetOutputToWaveFile($Caminho)
$sintetizador.Speak($texto)
$sintetizador.Dispose()

"{0}|{1}" -f $nomeUsado, $culturaUsada
