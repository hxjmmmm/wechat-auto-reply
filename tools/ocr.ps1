param([string]$ImagePath, [string]$OutPath)

$out = @()
function L($m) { $script:out += ($m | Out-String) }

try {
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        })[0]
    function Await($op, $type) {
        $m = $asTaskGeneric.MakeGenericMethod($type)
        $t = $m.Invoke($null, @($op))
        $t.Wait(-1) | Out-Null
        return $t.Result
    }

    $lang = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | Where-Object { $_.LanguageTag -like 'zh*' } | Select-Object -First 1
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)

    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

    L "OCR_OK"
    L $result.Text
}
catch {
    L "OCR_FAIL"
    L $_.Exception.Message
}

$target = if ($OutPath) { $OutPath } else { Join-Path $PSScriptRoot "_ocr_out.txt" }
Set-Content -Path $target -Value $out -Encoding UTF8
