param(
    [Parameter(Mandatory = $true)]
    [string]$BinDir
)

$bin = [System.IO.Path]::GetFullPath($BinDir.TrimEnd("\", "/"))
$user = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $user) { $user = "" }
$parts = @($user -split ";" | Where-Object { $_ -ne "" })
$normalized = @($parts | ForEach-Object { $_.TrimEnd("\", "/").ToLowerInvariant() })
if ($normalized -contains $bin.ToLowerInvariant()) {
    Write-Output "PATH already has $bin"
    exit 0
}
$next = if ([string]::IsNullOrWhiteSpace($user)) { $bin } else { $user.TrimEnd(";") + ";" + $bin }
[Environment]::SetEnvironmentVariable("Path", $next, "User")
Write-Output "Added to user PATH: $bin"
