function Initialize-NameRecognizer {
    if ($script:nameRecognizerStatus) { return }
    try {
        $modelPath = Join-Path $PSScriptRoot 'ch_PP-OCRv4_rec_infer.onnx'
        if (!(Test-Path -LiteralPath $modelPath -PathType Leaf)) { throw 'model_missing' }
        $null = [Windows.AI.MachineLearning.LearningModel,Windows.AI.MachineLearning,ContentType=WindowsRuntime]
        $null = [Windows.AI.MachineLearning.LearningModelDevice,Windows.AI.MachineLearning,ContentType=WindowsRuntime]
        $null = [Windows.AI.MachineLearning.LearningModelSession,Windows.AI.MachineLearning,ContentType=WindowsRuntime]
        $null = [Windows.AI.MachineLearning.LearningModelBinding,Windows.AI.MachineLearning,ContentType=WindowsRuntime]
        $null = [Windows.AI.MachineLearning.TensorFloat,Windows.AI.MachineLearning,ContentType=WindowsRuntime]
        Add-Type -Path (Join-Path $PSScriptRoot 'name_ocr.cs') -ReferencedAssemblies System.Drawing
        $model = [Windows.AI.MachineLearning.LearningModel]::LoadFromFilePath($modelPath)
        foreach ($pair in $model.Metadata) {
            if ($pair.Key -eq 'character') { $script:recognizerAlphabet = [string[]](@('') + $pair.Value.Split([char]10) + @(' ')) }
        }
        if ($script:recognizerAlphabet.Length -ne 6625) { throw 'invalid_alphabet' }
        $device = [Windows.AI.MachineLearning.LearningModelDevice]::new([Windows.AI.MachineLearning.LearningModelDeviceKind]::Cpu)
        try {
            $options = [Windows.AI.MachineLearning.LearningModelSessionOptions,Windows.AI.MachineLearning,ContentType=WindowsRuntime]::new()
            [BAScoutOCR.NameRecognizer]::UseOneCpuThread($options)
            $script:nameRecognizerSession = [Windows.AI.MachineLearning.LearningModelSession]::new($model,$device,$options)
        } catch {
            # Older Windows builds expose WinML without native thread options.
            $script:nameRecognizerSession = [Windows.AI.MachineLearning.LearningModelSession]::new($model,$device)
        }
        $script:recognizerIn = $model.InputFeatures[0].Name
        $script:recognizerOut = $model.OutputFeatures[0].Name
        $script:nameRecognizerStatus = 'ready'
    } catch {
        $script:nameRecognizerStatus = 'unavailable'
        [Console]::Error.WriteLine('Secondary name recognition unavailable: ' + $_.Exception.Message)
    }
}

function Invoke-NameRecognition($row) {
    $inputTensor = [Windows.AI.MachineLearning.TensorFloat]::CreateFromArray([long[]]@(1,3,48,$row.width),$row.pixels)
    $outputTensor = $null
    try {
        $binding = [Windows.AI.MachineLearning.LearningModelBinding]::new($script:nameRecognizerSession)
        $binding.Bind($script:recognizerIn,$inputTensor)
        $result = $script:nameRecognizerSession.Evaluate($binding,'name')
        foreach ($pair in $result.Outputs) {
            if ($pair.Key -eq $script:recognizerOut) { $outputTensor = $pair.Value }
        }
        $shape = @($outputTensor.Shape)
        if ($shape.Count -ne 3 -or $shape[0] -ne 1 -or $shape[2] -ne $script:recognizerAlphabet.Length) { throw 'invalid_output_shape' }
        [BAScoutOCR.NameRecognizer]::Decode($outputTensor,$shape[1],$script:recognizerAlphabet)
    } finally {
        $inputTensor.Dispose()
        if ($null -ne $outputTensor) { $outputTensor.Dispose() }
    }
}

function Test-NameClipped($row, [string]$text) {
    # Some game nameplates draw their own ellipsis inside the crop.
    $row.clipped -or $text.EndsWith('..') -or $text.EndsWith([string][char]0x2026)
}

function Read-AuxiliaryNames([string]$path, $regions) {
    Initialize-NameRecognizer
    $script:nameFrameError = $false
    if ($script:nameRecognizerStatus -ne 'ready') { return }
    $source = $null
    $readings = @()
    try {
        $source = [Drawing.Bitmap]::new($path)
        foreach ($region in $regions) {
            $box = [double[]]$region.bbox
            $row = [BAScoutOCR.NameRecognizer]::Prepare($source,$box,$false)
            if ($null -eq $row) { continue }
            $primary = Invoke-NameRecognition $row
            $reading = @{team=$region.team;index=[int]$region.index;text=$primary.text;
                         confidence=[Math]::Round($primary.confidence,5);clipped=(Test-NameClipped $row $primary.text)}
            # The extra view costs one model evaluation, so only use it on uncertain, complete names.
            if ($primary.confidence -lt 0.96 -and !$reading.clipped -and $primary.text -notmatch '^player\s*name$') {
                try {
                    $softRow = [BAScoutOCR.NameRecognizer]::Prepare($source,$box,$true)
                    if ($null -ne $softRow) {
                        $alternate = Invoke-NameRecognition $softRow
                        if ($alternate.text) {
                            $reading.alternate_text = $alternate.text
                            $reading.alternate_confidence = [Math]::Round($alternate.confidence,5)
                            $reading.alternate_clipped = Test-NameClipped $softRow $alternate.text
                        }
                    }
                } catch {
                    [Console]::Error.WriteLine('Alternate name view failed: ' + $_.Exception.Message)
                }
            }
            if ($primary.text -or $reading.alternate_text) { $readings += $reading }
        }
        $readings
    } catch {
        $script:nameFrameError = $true
        [Console]::Error.WriteLine('Secondary name recognition failed: ' + $_.Exception.Message)
    } finally {
        if ($null -ne $source) { $source.Dispose() }
    }
}
