param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string] $InputFile,

    [Parameter(Position = 1)]
    [string] $OutputDirectory,

    [switch] $Force
)

$ErrorActionPreference = "Stop"
$BeginPrefix = "<<<CLIPBOARD_TYPER_FILE_BEGIN:"
$BeginSuffix = ">>>"
$EndMarker = "<<<CLIPBOARD_TYPER_FILE_END>>>"

$InputPath = (Resolve-Path -LiteralPath $InputFile).Path
if (-not $OutputDirectory) {
    $Parent = [IO.Path]::GetDirectoryName($InputPath)
    $Stem = [IO.Path]::GetFileNameWithoutExtension($InputPath)
    $OutputDirectory = [IO.Path]::Combine($Parent, $Stem + "-files")
}
$OutputPath = [IO.Path]::GetFullPath($OutputDirectory)
$Text = [IO.File]::ReadAllText($InputPath)

$Pattern = ('(?ms)^' + [regex]::Escape($BeginPrefix) +
    '(?<name>[^\r\n<>:]+)' + [regex]::Escape($BeginSuffix) +
    '\r?\n(?<payload>[A-Za-z0-9+/=\r\n]*)\r?\n' +
    [regex]::Escape($EndMarker) + '(?:\r?\n|$)')
$Matches = [regex]::Matches($Text, $Pattern)
if ($Matches.Count -eq 0) {
    throw "No ClipboardTyper file blocks were found in '$InputPath'."
}

$Records = New-Object System.Collections.Generic.List[object]
$Names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$Cursor = 0
foreach ($Match in $Matches) {
    if ($Match.Index -ne $Cursor) {
        throw "Unexpected or damaged text at character $Cursor."
    }
    $Cursor = $Match.Index + $Match.Length
    $Name = $Match.Groups['name'].Value
    if ([IO.Path]::GetFileName($Name) -ne $Name) {
        throw "Unsafe file name in transfer text: '$Name'."
    }
    if (-not $Names.Add($Name)) {
        throw "Duplicate file name in transfer text: '$Name'."
    }
    $Payload = [regex]::Replace($Match.Groups['payload'].Value, '\s', '')
    try {
        $Bytes = [Convert]::FromBase64String($Payload)
    }
    catch {
        throw "Invalid Base64 data for '$Name'."
    }
    $Records.Add([pscustomobject]@{ Name = $Name; Bytes = $Bytes })
}
if ($Cursor -ne $Text.Length) {
    throw "Unexpected or damaged text at character $Cursor."
}

foreach ($Record in $Records) {
    $Destination = [IO.Path]::Combine($OutputPath, $Record.Name)
    if ((Test-Path -LiteralPath $Destination) -and -not $Force) {
        throw "Output file already exists: '$Destination'. Use -Force to overwrite it."
    }
}

[IO.Directory]::CreateDirectory($OutputPath) | Out-Null
foreach ($Record in $Records) {
    $Destination = [IO.Path]::Combine($OutputPath, $Record.Name)
    [IO.File]::WriteAllBytes($Destination, $Record.Bytes)
    Write-Host "Created: $Destination"
}
Write-Host "Restored $($Records.Count) file(s) in: $OutputPath"
