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

function Read-AuxiliaryNames([string]$path, $regions) {
    Initialize-NameRecognizer
    $script:nameFrameError = $false
    if ($script:nameRecognizerStatus -ne 'ready') { return }
    $source = $null
    $readings = @()
    try {
        $source = [Drawing.Bitmap]::new($path)
        foreach ($region in $regions) {
            $row = [BAScoutOCR.NameRecognizer]::Prepare($source,[double[]]$region.bbox)
            if ($null -eq $row) { continue }
            $inputTensor = [Windows.AI.MachineLearning.TensorFloat]::CreateFromArray([long[]]@(1,3,48,$row.width),$row.pixels)
            $binding = [Windows.AI.MachineLearning.LearningModelBinding]::new($script:nameRecognizerSession)
            try {
                $binding.Bind($script:recognizerIn,$inputTensor)
                $result = $script:nameRecognizerSession.Evaluate($binding,'name')
                foreach ($pair in $result.Outputs) {
                    if ($pair.Key -eq $script:recognizerOut) { $outputTensor = $pair.Value }
                }
                $shape = @($outputTensor.Shape)
                if ($shape.Count -ne 3 -or $shape[0] -ne 1 -or $shape[2] -ne $script:recognizerAlphabet.Length) { throw 'invalid_output_shape' }
                $decoded = [BAScoutOCR.NameRecognizer]::Decode($outputTensor,$shape[1],$script:recognizerAlphabet)
                if ($decoded.text) {
                    # Some game nameplates draw their own ellipsis inside the crop.
                    $truncated = $row.clipped -or $decoded.text.EndsWith('..') -or $decoded.text.EndsWith([string][char]0x2026)
                    $readings += @{team=$region.team;index=[int]$region.index;text=$decoded.text;
                                   confidence=[Math]::Round($decoded.confidence,5);clipped=$truncated}
                }
            } finally {
                $inputTensor.Dispose()
                if ($null -ne $outputTensor) { $outputTensor.Dispose(); $outputTensor=$null }
            }
        }
        $readings
    } catch {
        $script:nameFrameError = $true
        [Console]::Error.WriteLine('Secondary name recognition failed: ' + $_.Exception.Message)
    } finally {
        if ($null -ne $source) { $source.Dispose() }
    }
}
