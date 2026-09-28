# Janela de teste do driver de GUI (tests/test_gui.py): WinForms pelo Windows PowerShell.
# Registra no arquivo -Log cada tecla, clique, arrasto, item de menu e resposta de diálogo.
param(
    [Parameter(Mandatory = $true)][string]$Log,
    [switch]$Cobrir,
    [switch]$Sujo,
    [string]$Titulo = "Janela de teste GUI"
)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
# Pixels físicos, como o aplicativo testado (sem isso o Windows escala as coordenadas postadas).
Add-Type -Namespace AppTeste -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
[void][AppTeste.Dpi]::SetProcessDPIAware()
[System.Windows.Forms.Application]::EnableVisualStyles()

function Registrar([string]$t) {
    [System.IO.File]::AppendAllText($Log, $t + [Environment]::NewLine, [System.Text.Encoding]::UTF8)
}

$form = New-Object System.Windows.Forms.Form
$form.Text = $Titulo
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point(140, 140)
$form.Size = New-Object System.Drawing.Size(560, 380)
$form.BackColor = [System.Drawing.Color]::FromArgb(200, 30, 120)
$form.KeyPreview = $true
$script:sujo = [bool]$Sujo

$form.Add_KeyDown({ param($s, $e) Registrar ("down:" + $e.KeyCode + ":" + $e.Modifiers); $script:sujo = $true })
$form.Add_KeyPress({ param($s, $e) Registrar ("char:" + [int][char]$e.KeyChar) })
# O WinForms tira o botão do MouseMove do mouse físico; o arrasto postado é seguido entre down e up.
$script:apertado = $false
# Modificadores lidos do estado de teclado da thread (GetKeyState), como o WinForms faz.
$form.Add_MouseDown({ param($s, $e) $script:apertado = $true
    $m = [System.Windows.Forms.Control]::ModifierKeys
    if ($m -ne [System.Windows.Forms.Keys]::None) { Registrar ("mods:" + $m) }
    Registrar ("mousedown:" + $e.Button + ":" + $e.X + "," + $e.Y + ":" + $e.Clicks) })
$form.Add_MouseUp({ param($s, $e) $script:apertado = $false; Registrar ("mouseup:" + $e.Button + ":" + $e.X + "," + $e.Y) })
$form.Add_MouseMove({ param($s, $e) if ($script:apertado) { Registrar ("drag:" + $e.X + "," + $e.Y) } })

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$primeiro = New-Object System.Windows.Forms.ToolStripMenuItem("Primeiro")
$primeiro.Add_Click({ Registrar "menu:Primeiro" })
$sub = New-Object System.Windows.Forms.ToolStripMenuItem("Submenu")
$interno = New-Object System.Windows.Forms.ToolStripMenuItem("Interno")
$interno.Add_Click({ Registrar "menu:Interno" })
[void]$sub.DropDownItems.Add($interno)
[void]$menu.Items.Add($primeiro)
[void]$menu.Items.Add($sub)

function NovoBotao([string]$texto, [int]$x, [int]$y) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $texto
    $b.Location = New-Object System.Drawing.Point($x, $y)
    $b.Size = New-Object System.Drawing.Size(110, 28)
    $b.BackColor = [System.Drawing.SystemColors]::Control
    $form.Controls.Add($b)
    return $b
}

$bMenu = NovoBotao "Abrir menu" 10 10
$bMenu.Add_Click({ $menu.Show($bMenu, 0, $bMenu.Height) })

# Caixas de mensagem com o ícone de pergunta: o som "Pergunta" do Windows é vazio no esquema padrão
# (a caixa sem ícone toca o som padrão). Diálogos modais vão por BeginInvoke: o clique (UIA Invoke) volta antes de a caixa abrir.
$bAviso = NovoBotao "Aviso" 130 10
$bAviso.Add_Click({
    [void]$form.BeginInvoke([Action]{
        $r = [System.Windows.Forms.MessageBox]::Show($form, "Mensagem de teste do aviso", "Aviso de teste", 'OKCancel', 'Question')
        Registrar ("aviso:" + $r)
    })
})

$bSalvar = NovoBotao "Salvar" 250 10
$bSalvar.Add_Click({
    [void]$form.BeginInvoke([Action]{
        $d = New-Object System.Windows.Forms.SaveFileDialog
        $d.Filter = "Texto (*.txt)|*.txt"
        if ($d.ShowDialog($form) -eq 'OK') { Registrar ("salvo:" + $d.FileName); Set-Content -Path $d.FileName -Value "x" }
        else { Registrar "salvo:cancelado" }
    })
})

$bTravar = NovoBotao "Travar" 370 10
$bTravar.Add_Click({ [void]$form.BeginInvoke([Action]{ [System.Threading.Thread]::Sleep(40000) }) })  # Sleep sem bombear mensagens

$opcao = New-Object System.Windows.Forms.CheckBox
$opcao.Text = "Opcao"
$opcao.Location = New-Object System.Drawing.Point(10, 50)
$opcao.BackColor = [System.Drawing.SystemColors]::Control
$opcao.Add_CheckedChanged({ Registrar ("opcao:" + $opcao.Checked) })
$form.Controls.Add($opcao)

$nivel = New-Object System.Windows.Forms.TrackBar
$nivel.Location = New-Object System.Drawing.Point(130, 50)
$nivel.Minimum = 0
$nivel.Maximum = 100
$nivel.Value = 25
$nivel.AccessibleName = "Nivel"
$form.Controls.Add($nivel)

$form.Add_FormClosing({
    param($s, $e)
    if ($script:sujo) {
        $r = [System.Windows.Forms.MessageBox]::Show($form, "Salvar as alteracoes antes de sair?", "Fechar teste", 'YesNoCancel', 'Question')
        Registrar ("fechar:" + $r)
        if ($r -eq 'Cancel') { $e.Cancel = $true }
    }
})

$form.Add_Shown({
    Registrar "pronto"
    if ($Cobrir) {
        $capa = New-Object System.Windows.Forms.Form
        $capa.Text = "Capa de teste"
        $capa.StartPosition = 'Manual'
        $capa.Location = $form.Location
        $capa.Size = $form.Size
        $capa.BackColor = [System.Drawing.Color]::FromArgb(20, 200, 40)
        $capa.TopMost = $true
        $capa.ShowInTaskbar = $false
        $capa.Show()
    }
})

[System.Windows.Forms.Application]::Run($form)
