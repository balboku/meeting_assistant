#Requires -RunAsAdministrator

param(
    [string]$TrustedSubnet = "192.168.20.0/24",
    [ValidateRange(1, 65535)]
    [int]$Port = 8001,
    [switch]$Disable
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RuleName = "Meeting Assistant LAN $Port"
$ExistingRule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue

if ($Disable) {
    if ($ExistingRule) {
        Disable-NetFirewallRule -DisplayName $RuleName
    }
    Write-Host "Disabled firewall rule: $RuleName"
    exit 0
}

if ($TrustedSubnet -notmatch "^[0-9a-fA-F:.]+/[0-9]{1,3}$") {
    throw "TrustedSubnet must be one CIDR, for example 192.168.20.0/24."
}

if (-not $ExistingRule) {
    New-NetFirewallRule `
        -DisplayName $RuleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port `
        -Profile Any `
        -RemoteAddress $TrustedSubnet | Out-Null
}
else {
    Set-NetFirewallRule `
        -DisplayName $RuleName `
        -Enabled True `
        -Direction Inbound `
        -Action Allow `
        -Profile Any
    Get-NetFirewallRule -DisplayName $RuleName |
        Get-NetFirewallAddressFilter |
        Set-NetFirewallAddressFilter -RemoteAddress $TrustedSubnet
    Get-NetFirewallRule -DisplayName $RuleName |
        Get-NetFirewallPortFilter |
        Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Port
}

Write-Host "Enabled firewall rule: $RuleName"
Write-Host "Allowed source only: $TrustedSubnet"
Write-Host "Persistent URL: http://NB-RD-BALBO:$Port/history"
