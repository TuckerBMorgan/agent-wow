param([switch]$PrepareImage)
$ErrorActionPreference = 'Stop'

if (!$PrepareImage) {
    # Windows Setup runs this on each new instance before the first desktop login.
    $password = [Guid]::NewGuid().ToString('N') + '!aA1'
    New-LocalUser Viewer -Password (ConvertTo-SecureString $password -AsPlainText -Force) -PasswordNeverExpires | Out-Null
    $winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    Set-ItemProperty $winlogon DefaultUserName 'Viewer'
    Set-ItemProperty $winlogon DefaultDomainName $env:COMPUTERNAME
    Set-ItemProperty $winlogon DefaultPassword $password
    Remove-ItemProperty $winlogon AutoLogonCount -ErrorAction SilentlyContinue
    Set-ItemProperty $winlogon AutoAdminLogon '1'
    return
}

# Prepare a clean clone of the existing viewer AMI before running EC2Launch Sysprep.
if (Get-LocalUser Viewer -ErrorAction SilentlyContinue) { throw 'The builder already contains a Viewer user.' }
foreach ($name in 'settings.json', 'account.json', 'job.json', 'configured.flag') {
    if (Test-Path "C:\viewer\$name") { throw "The builder contains viewer runtime state: $name" }
}
$path = 'C:\ProgramData\Amazon\EC2Launch\sysprep\unattend.xml'
$xml = [xml](Get-Content $path -Raw)
$unattend = 'urn:schemas-microsoft-com:unattend'
$namespaces = [System.Xml.XmlNamespaceManager]::new($xml.NameTable)
$namespaces.AddNamespace('u', $unattend)
$commands = $xml.SelectSingleNode("//u:settings[@pass='specialize']/u:component[@name='Microsoft-Windows-Deployment']/u:RunSynchronous", $namespaces)
if (!$commands) { throw 'The EC2Launch answer file has no specialize commands.' }
if ($commands.SelectSingleNode("u:RunSynchronousCommand[u:Description='Create viewer desktop account']", $namespaces)) { return }

$orders = $commands.SelectNodes('u:RunSynchronousCommand/u:Order', $namespaces) | ForEach-Object { [int]$_.InnerText }
$order = ($orders | Measure-Object -Maximum).Maximum + 1
$command = $xml.CreateElement('RunSynchronousCommand', $unattend)
$action = $xml.CreateAttribute('wcm', 'action', 'http://schemas.microsoft.com/WMIConfig/2002/State')
$action.Value = 'add'
[void]$command.Attributes.Append($action)
$fields = [ordered]@{
    Order = $order
    Description = 'Create viewer desktop account'
    Path = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\viewer\first-boot-signin.ps1'
}
foreach ($name in $fields.Keys) {
    $field = $xml.CreateElement($name, $unattend)
    $field.InnerText = [string]$fields[$name]
    [void]$command.AppendChild($field)
}
[void]$commands.AppendChild($command)
$xml.Save($path)
Write-Output "Installed the first-boot desktop setup at specialize order $order."
