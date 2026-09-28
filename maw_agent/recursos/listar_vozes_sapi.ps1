# Lista as vozes SAPI (System.Speech) instaladas: uma linha "Nome|Cultura|Ativa" por voz.
Add-Type -AssemblyName System.Speech
$sintetizador = New-Object System.Speech.Synthesis.SpeechSynthesizer
foreach ($voz in $sintetizador.GetInstalledVoices()) {
    $info = $voz.VoiceInfo
    "{0}|{1}|{2}" -f $info.Name, $info.Culture.Name, $voz.Enabled
}
$sintetizador.Dispose()
