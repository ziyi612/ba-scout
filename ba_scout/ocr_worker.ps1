param(
    [Parameter(Mandatory = $true)][string]$TempRoot,
    [Parameter(Mandatory = $true)][int]$ClientPid
)
# This file is distributed with a UTF-8 BOM for Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

function Send-Json($value) {
    [Console]::Out.WriteLine(($value | ConvertTo-Json -Depth 18 -Compress))
    [Console]::Out.Flush()
}
function Assert-AbsolutePath([string]$value) {
    if (![IO.Path]::IsPathRooted($value) -or [IO.Path]::GetPathRoot($value).Length -lt 3) {
        throw 'OCR requires an absolute path.'
    }
    return [IO.Path]::GetFullPath($value)
}

try {
    if ($PSVersionTable.PSEdition -ne 'Desktop') { throw 'Windows PowerShell 5.1 is required.' }
    $TempRoot = Assert-AbsolutePath $TempRoot
    $null = [IO.Directory]::CreateDirectory($TempRoot)
    # Python validates TempRoot with settings.app_path before starting us.
    # Set these before Add-Type so CodeDom uses the application-owned folder.
    $env:TEMP = $TempRoot
    $env:TMP = $TempRoot
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $nativeSource = @'
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
namespace BAScoutOCR {
  public sealed class RegionTile {
    public int roi_index;
    public string preprocessing;
    public int[] source_bbox, montage_bbox;
  }
  public sealed class RegionMontage {
    public int image_width, image_height, montage_width, montage_height;
    public double scale;
    public RegionTile[] tiles;
  }
  public static class NameRegions {
    public static RegionMontage Build(string input, string output, double[][] boxes, int limit, int mode) {
      if (boxes==null || boxes.Length!=10 || limit<256 || limit>8192) throw new ArgumentException("Ten name regions are required.");
      using(var source=new Bitmap(input)) {
        if (source.Width>8192 || source.Height>8192 || (long)source.Width*source.Height>16777216)
          throw new ArgumentException("Source image exceeds the OCR dimension limit.");
        var bounds=new Rectangle[boxes.Length];
        int maximumWidth=0, totalHeight=0;
        for(int i=0;i<boxes.Length;i++) {
          var b=boxes[i];
          if(b==null || b.Length!=4) throw new ArgumentException("Malformed name region.");
          foreach(double value in b) if(Double.IsNaN(value) || Double.IsInfinity(value)) throw new ArgumentException("Non-finite region.");
          if(b[0]<0 || b[1]<0 || b[2]<=0 || b[3]<=0 || b[0]+b[2]>1.000000001 || b[1]+b[3]>1.000000001)
            throw new ArgumentException("Name region is outside the original image.");
          int x=Math.Max(0,(int)Math.Floor(b[0]*source.Width+1e-7));
          int y=Math.Max(0,(int)Math.Floor(b[1]*source.Height+1e-7));
          int right=Math.Min(source.Width,(int)Math.Ceiling((b[0]+b[2])*source.Width-1e-7));
          int bottom=Math.Min(source.Height,(int)Math.Ceiling((b[1]+b[3])*source.Height-1e-7));
          if(right<=x || bottom<=y) throw new ArgumentException("Name region has no pixels.");
          bounds[i]=new Rectangle(x,y,right-x,bottom-y);
          maximumWidth=Math.Max(maximumWidth,right-x); totalHeight+=bottom-y;
        }
        // Preserve the original two-view atlas exactly; supplemental 120 uses its own atlas.
        // Scaling may decrease for unusually large calibrated boxes.
        const int pad=24, gap=24;
        int views=mode==0?2:1;
        double scale=Math.Min(3.0,Math.Min((limit-2.0*pad)/maximumWidth,
          (limit-2.0*pad-gap*(boxes.Length*views-1))/(totalHeight*views)));
        if(scale<=0) throw new ArgumentException("Name regions cannot fit the bounded OCR montage.");
        int atlasWidth=(int)Math.Floor(maximumWidth*scale)+2*pad;
        int atlasHeight=2*pad+gap*(boxes.Length*views-1);
        foreach(var r in bounds) atlasHeight+=views*Math.Max(1,(int)Math.Floor(r.Height*scale));
        var tiles=new List<RegionTile>();
        using(var atlas=new Bitmap(atlasWidth,atlasHeight,PixelFormat.Format24bppRgb))
        using(var graphics=Graphics.FromImage(atlas)) {
          graphics.Clear(mode==2 ? Color.White : Color.Black);
          int top=pad;
          for(int variant=0;variant<views;variant++) for(int i=0;i<bounds.Length;i++) {
            int kind=mode==0?variant:mode+1;
            Rectangle sourceBox=bounds[i];
            int width=Math.Max(1,(int)Math.Floor(sourceBox.Width*scale));
            int height=Math.Max(1,(int)Math.Floor(sourceBox.Height*scale));
            using(var tile=new Bitmap(width,height,PixelFormat.Format24bppRgb)) {
              using(var ink=kind==3 ? new Bitmap(sourceBox.Width,sourceBox.Height,PixelFormat.Format24bppRgb) : null)
              using(var g=Graphics.FromImage(tile)) {
                g.InterpolationMode=System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
                g.PixelOffsetMode=System.Drawing.Drawing2D.PixelOffsetMode.HighQuality;
                if(ink==null) {
                  g.DrawImage(source,new Rectangle(0,0,width,height),sourceBox,GraphicsUnit.Pixel);
                } else {
                  // Preserve antialiased stems and underscores before enlarging them.
                  for(int y=0;y<ink.Height;y++) for(int x=0;x<ink.Width;x++) {
                    Color c=source.GetPixel(sourceBox.X+x,sourceBox.Y+y);
                    int light=Math.Min(c.R,Math.Min(c.G,c.B));
                    int gray=255-Math.Min(255,Math.Max(0,(light-155)*255/60));
                    ink.SetPixel(x,y,Color.FromArgb(gray,gray,gray));
                  }
                  g.Clear(Color.White);
                  g.DrawImage(ink,new Rectangle(0,0,width,height),new Rectangle(0,0,ink.Width,ink.Height),GraphicsUnit.Pixel);
                }
              }
              if(kind==1 || kind==2) {
                int threshold=kind==1?140:120;
                // Names in this layout use bright text on illustrated banners.
                // Keep the original-colour variant too; never replace its text.
                for(int y=0;y<height;y++) for(int x=0;x<width;x++) {
                  Color c=tile.GetPixel(x,y);
                  tile.SetPixel(x,y,c.R>=threshold && c.G>=threshold && c.B>=threshold?Color.Black:Color.White);
                }
                graphics.FillRectangle(Brushes.White,0,top-gap/2,atlasWidth,height+gap);
              }
              graphics.DrawImageUnscaled(tile,pad,top);
            }
            tiles.Add(new RegionTile {roi_index=i,preprocessing=kind==0?"colour_3x":kind==1?"light_text_140_3x":kind==2?"light_text_120_3x":"soft_text_185_3x",
              source_bbox=new[]{sourceBox.X,sourceBox.Y,sourceBox.Width,sourceBox.Height},
              montage_bbox=new[]{pad,top,width,height}});
            top+=height+gap;
          }
          atlas.Save(output,ImageFormat.Png);
        }
        return new RegionMontage {image_width=source.Width,image_height=source.Height,
          montage_width=atlasWidth,montage_height=atlasHeight,scale=scale,tiles=tiles.ToArray()};
      }
    }
  }
  public sealed class CaptureResult {
    public bool ok;
    public string status, error, image_path, title, exe_name;
    public int image_width, image_height, client_x, client_y, process_id;
    public long hwnd, foreground_hwnd;
  }
  public static class NativeCapture {
    [StructLayout(LayoutKind.Sequential)] struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] struct POINT { public int X, Y; }
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindowEx(IntPtr p, IntPtr after, string cls, string title);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint id);
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, uint id);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder name, ref uint size);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr h, uint flags);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr h, uint cmd);
    [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr h, uint attr, out int value, int size);
    static string Executable(IntPtr hwnd, out uint processId) {
      GetWindowThreadProcessId(hwnd, out processId);
      IntPtr process = OpenProcess(0x1000, false, processId);
      if (process == IntPtr.Zero) return null;
      try {
        uint length=32768; var name=new StringBuilder((int)length);
        return QueryFullProcessImageName(process, 0, name, ref length) ? Path.GetFileName(name.ToString()) : null;
      } finally { CloseHandle(process); }
    }
    static CaptureResult Fail(string status, string message) {
      return new CaptureResult { ok=false, status=status, error=message };
    }
    // The overlay can receive focus while the user reads the cards. Both
    // focus checks still capture the verified game HWND only, never the desktop.
    static bool EligibleForeground(IntPtr foreground, IntPtr game, int owner) {
      if (foreground==IntPtr.Zero) return false;
      IntPtr root=GetAncestor(foreground,2);
      if (root==IntPtr.Zero) return false;
      if (root==game) return true;
      uint processId;
      return owner>0 && GetWindowThreadProcessId(root,out processId)!=0 && processId==(uint)owner;
    }
    static bool Occluded(IntPtr game, RECT client, int owner) {
      int guard=0;
      for (IntPtr h=GetWindow(game,3); h!=IntPtr.Zero && guard++<4096; h=GetWindow(h,3)) {
        if (!IsWindowVisible(h) || IsIconic(h)) continue;
        uint other; GetWindowThreadProcessId(h,out other);
        // PrintWindow does not include our overlay. Do not examine other apps' text.
        if (other==(uint)owner) continue;
        int cloaked;
        if (DwmGetWindowAttribute(h,14,out cloaked,4)==0 && cloaked!=0) continue;
        RECT r;
        if (GetWindowRect(h,out r) && r.Right>client.Left && r.Left<client.Right &&
            r.Bottom>client.Top && r.Top<client.Bottom) return true;
      }
      return false;
    }
    public static CaptureResult Capture(string output, bool requireForeground, int owner) {
      try { SetProcessDpiAwarenessContext(new IntPtr(-4)); }
      catch (EntryPointNotFoundException) { SetProcessDPIAware(); }
      var candidates=new Dictionary<IntPtr,string>();
      foreach (string title in new [] {"Broken Arrow","BrokenArrow"}) {
        IntPtr after=IntPtr.Zero;
        for (int i=0;i<64;i++) {
          IntPtr found=FindWindowEx(IntPtr.Zero,after,null,title);
          if (found==IntPtr.Zero) break;
          after=found; uint id;
          if (String.Equals(Executable(found,out id),"BrokenArrow.exe",StringComparison.OrdinalIgnoreCase)) candidates[found]=title;
        }
      }
      if (candidates.Count==0) return Fail("not_found","No exact BrokenArrow.exe game window was found.");
      if (candidates.Count!=1) return Fail("ambiguous_window","More than one verified game window is present.");
      IntPtr game=IntPtr.Zero; string gameTitle=null;
      foreach (var item in candidates) { game=item.Key; gameTitle=item.Value; }
      if (IsIconic(game)) return Fail("minimized","The game window is minimized.");
      if (!IsWindowVisible(game)) return Fail("not_visible","The game window is not visible.");
      IntPtr foreground=GetForegroundWindow();
      if (requireForeground && !EligibleForeground(foreground,game,owner)) return Fail("not_foreground","Switch back to Broken Arrow or its scout overlay.");
      RECT r; POINT p=new POINT();
      if (!GetClientRect(game,out r) || !ClientToScreen(game,ref p)) return Fail("capture_failed","Game client bounds are unavailable.");
      int width=r.Right-r.Left, height=r.Bottom-r.Top;
      if (width<320 || height<200 || width>4096 || height>4096 || (long)width*height>12000000)
        return Fail("unsupported_dimensions","Game client dimensions are outside the capture limit.");
      RECT screen=new RECT {Left=p.X,Top=p.Y,Right=p.X+width,Bottom=p.Y+height};
      if (Occluded(game,screen,owner)) return Fail("occluded","Another application's window overlaps the game client.");
      uint processId; string exe=Executable(game,out processId);
      if (!String.Equals(exe,"BrokenArrow.exe",StringComparison.OrdinalIgnoreCase)) return Fail("window_changed","The game window identity changed.");
      // GDI PrintWindow does not reliably set alpha; an RGB bitmap keeps the
      // saved calibration PNG opaque in Tk as well as in WinRT OCR.
      using (var bitmap=new Bitmap(width,height,PixelFormat.Format24bppRgb)) {
        using (var graphics=Graphics.FromImage(bitmap)) {
          graphics.Clear(Color.Black);
          IntPtr dc=graphics.GetHdc(); bool printed;
          try { printed=PrintWindow(game,dc,3); } finally { graphics.ReleaseHdc(dc); }
          if (!printed) return Fail("capture_failed","PrintWindow did not return an image.");
        }
        if (IsIconic(game) || (requireForeground && !EligibleForeground(GetForegroundWindow(),game,owner)))
          return Fail("window_changed","The game lost its capture eligibility during PrintWindow.");
        uint afterId;
        if (!String.Equals(Executable(game,out afterId),"BrokenArrow.exe",StringComparison.OrdinalIgnoreCase) || afterId!=processId)
          return Fail("window_changed","The game window identity changed during capture.");
        RECT afterBounds;
        if (!GetClientRect(game,out afterBounds) || afterBounds.Right-afterBounds.Left!=width || afterBounds.Bottom-afterBounds.Top!=height)
          return Fail("window_changed","The game client resized during capture.");
        if (Occluded(game,screen,owner)) return Fail("occluded","Another application's window overlaps the game client.");
        int count=0, dark=0; double sum=0, sum2=0;
        for (int y=0;y<height;y+=Math.Max(1,height/36)) for (int x=0;x<width;x+=Math.Max(1,width/64)) {
          Color c=bitmap.GetPixel(x,y); double level=(c.R+c.G+c.B)/3.0;
          if (Math.Max(c.R,Math.Max(c.G,c.B))<8) dark++;
          count++; sum+=level; sum2+=level*level;
        }
        double variance=sum2/count-(sum/count)*(sum/count);
        if ((double)dark/count>0.995 || variance<1)
          return Fail("black_frame","The capture is black or nearly uniform; no OCR was attempted.");
        bitmap.Save(output,ImageFormat.Png);
      }
      return new CaptureResult {ok=true,status="ok",image_path=output,image_width=width,image_height=height,
        client_x=p.X,client_y=p.Y,process_id=(int)processId,hwnd=game.ToInt64(),foreground_hwnd=foreground.ToInt64(),
        title=gameTitle,exe_name=exe};
    }
  }
}
'@
    Add-Type -TypeDefinition $nativeSource -ReferencedAssemblies System.Drawing
    $null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
    $null = [Windows.Storage.Streams.IRandomAccessStreamWithContentType,Windows.Storage.Streams,ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapTransform,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine,Windows.Media.Ocr,ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrResult,Windows.Media.Ocr,ContentType=WindowsRuntime]
    $null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $availableLanguages = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag })
    $engines = @{}
    $captures = New-Object 'System.Collections.Generic.Queue[string]'
    Send-Json @{type='ready'; protocol=1; available_languages=$availableLanguages; max_dimension=[Windows.Media.Ocr.OcrEngine]::MaxImageDimension}
} catch {
    Send-Json @{type='fatal'; error=$_.Exception.Message}
    exit 1
}

function Wait-WinRT($operation, [Type]$returnType) {
    $task = $asTask.MakeGenericMethod($returnType).Invoke($null, @($operation))
    $task.Wait()
    return $task.Result
}
function Initialize-OCRLanguages($requested) {
    # One engine per actual installed language, retained for this worker's life.
    # This path never opens images, enumerates windows, or captures the desktop.
    $used = @(); $missing = @(); $aliases = @{}; $created = 0; $reused = 0
    foreach ($requestedTag in $requested) {
        $tag = $requestedTag
        if ($tag -notin $availableLanguages) {
            # Preserve unique-primary-language matching, including ru-RU -> ru.
            $primary = ($tag -split '-')[0]
            $matches = @($availableLanguages | Where-Object { ($_ -split '-')[0] -eq $primary })
            if ($matches.Count -ne 1) { $missing += $requestedTag; continue }
            $tag = $matches[0]; $aliases[$requestedTag] = $tag
        }
        if ($tag -in $used) { continue }
        if (!$engines.ContainsKey($tag)) {
            $engines[$tag] = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language($tag)))
            if ($null -ne $engines[$tag]) { $created++ }
        } elseif ($null -ne $engines[$tag]) { $reused++ }
        if ($null -eq $engines[$tag]) { $missing += $tag; continue }
        $used += $tag
    }
    if ($used.Count -eq 0) { throw 'None of the requested OCR languages is installed.' }
    return @{available_languages=$availableLanguages; languages=$used; missing_languages=$missing;
        language_aliases=$aliases; engine_cache=@{created=$created;reused=$reused;total=$engines.Count}}
}
function Read-OCR([string]$path, $requested) {
    $path = Assert-AbsolutePath $path
    if ([IO.Path]::GetExtension($path) -ine '.png' -or ![IO.File]::Exists($path)) { throw 'An existing PNG at an absolute path is required.' }
    if ((Get-Item -LiteralPath $path).Length -gt 33554432) { throw 'PNG exceeds the 32 MiB input limit.' }
    $file = Wait-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Wait-WinRT ($file.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
    $bitmap = $null
    try {
        $decoder = Wait-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $width = [int]$decoder.PixelWidth; $height = [int]$decoder.PixelHeight
        if ($width -lt 1 -or $height -lt 1 -or $width -gt 8192 -or $height -gt 8192 -or [long]$width*$height -gt 16777216) {
            throw 'Image dimensions exceed the OCR decoding limit.'
        }
        $maximum = [int][Windows.Media.Ocr.OcrEngine]::MaxImageDimension
        $ratio = [Math]::Min(1.0, $maximum / [double][Math]::Max($width,$height))
        $transform = New-Object Windows.Graphics.Imaging.BitmapTransform
        $transform.ScaledWidth = [uint32][Math]::Max(1,[Math]::Floor($width*$ratio))
        $transform.ScaledHeight = [uint32][Math]::Max(1,[Math]::Floor($height*$ratio))
        $bitmap = Wait-WinRT ($decoder.GetSoftwareBitmapAsync(
            [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
            [Windows.Graphics.Imaging.BitmapAlphaMode]::Ignore, $transform,
            [Windows.Graphics.Imaging.ExifOrientationMode]::IgnoreExifOrientation,
            [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage)) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $sx = $width / [double]$bitmap.PixelWidth; $sy = $height / [double]$bitmap.PixelHeight
        $languageInfo = Initialize-OCRLanguages $requested
        $results = @(); $allWords = @()
        foreach ($tag in $languageInfo.languages) {
            $watch = [Diagnostics.Stopwatch]::StartNew()
            $ocr = Wait-WinRT ($engines[$tag].RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $lines = @(); $languageWords = @()
            foreach ($line in $ocr.Lines) {
                $words = @()
                foreach ($word in $line.Words) {
                    $rect = $word.BoundingRect
                    $item = [ordered]@{text=$word.Text; x=$rect.X*$sx; y=$rect.Y*$sy; width=$rect.Width*$sx; height=$rect.Height*$sy; language=$tag}
                    $words += $item; $languageWords += $item
                }
                if ($words.Count -gt 0) {
                    $left=($words | ForEach-Object {$_.x} | Measure-Object -Minimum).Minimum
                    $top=($words | ForEach-Object {$_.y} | Measure-Object -Minimum).Minimum
                    $right=($words | ForEach-Object {$_.x+$_.width} | Measure-Object -Maximum).Maximum
                    $bottom=($words | ForEach-Object {$_.y+$_.height} | Measure-Object -Maximum).Maximum
                    $lines += [ordered]@{text=$line.Text; bounding_box=@{x=$left;y=$top;width=$right-$left;height=$bottom-$top}; words=$words}
                }
            }
            $allWords += $languageWords
            $results += [ordered]@{language=$tag; seconds=[Math]::Round($watch.Elapsed.TotalSeconds,4); line_count=$lines.Count; word_count=$languageWords.Count; lines=$lines; words=$languageWords}
        }
        return @{image_path=$path; image_width=$width; image_height=$height; ocr_width=$bitmap.PixelWidth; ocr_height=$bitmap.PixelHeight;
            available_languages=$languageInfo.available_languages; languages=$languageInfo.languages;
            missing_languages=$languageInfo.missing_languages; language_aliases=$languageInfo.language_aliases;
            engine_cache=$languageInfo.engine_cache; results=$results; words=$allWords;
            coordinate_system='original_image_pixels'; confidence_available=$false}
    } finally {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

$auxiliaryScript = Join-Path $PSScriptRoot 'name_ocr.ps1'
if (Test-Path -LiteralPath $auxiliaryScript -PathType Leaf) {
    . $auxiliaryScript
} else {
    function Initialize-NameRecognizer { $script:nameRecognizerStatus = 'unavailable' }
    function Read-AuxiliaryNames($path, $regions) { Initialize-NameRecognizer }
}

function Read-RegionsOCR([string]$path, $requested, $regions) {
    $path = Assert-AbsolutePath $path
    if ([IO.Path]::GetExtension($path) -ine '.png' -or ![IO.File]::Exists($path)) { throw 'An existing PNG at an absolute path is required.' }
    if ((Get-Item -LiteralPath $path).Length -gt 33554432) { throw 'PNG exceeds the 32 MiB input limit.' }
    if (@($regions).Count -ne 10) { throw 'Ten calibrated name regions are required.' }
    $boxes = New-Object 'System.Collections.Generic.List[double[]]'
    foreach ($region in $regions) { $boxes.Add([double[]]$region.bbox) }
    $montagePath = Join-Path $TempRoot ('ocr-regions-' + [Guid]::NewGuid().ToString('N') + '.png')
    try {
        $limit = [Math]::Min(2600,[int][Windows.Media.Ocr.OcrEngine]::MaxImageDimension)
        $mappedResults = @(); $allWords = @(); $allTiles = @(); $montages = @()
        $response = $null; $firstLayout = $null; $totalSeconds = 0.0
        foreach ($viewMode in @(0,1,2)) {
        $layout = [BAScoutOCR.NameRegions]::Build($path,$montagePath,$boxes.ToArray(),$limit,$viewMode)
        $pass = Read-OCR $montagePath $requested
        if ($null -eq $response) { $response=$pass; $firstLayout=$layout }
        $totalSeconds += [double]$pass.seconds
        $allTiles += $layout.tiles
        $viewNames = if ($viewMode -eq 0) { @('colour_3x','light_text_140_3x') } elseif ($viewMode -eq 1) { @('light_text_120_3x') } else { @('soft_text_185_3x') }
        $montages += @{preprocessing=$viewNames;dimensions=@($layout.montage_width,$layout.montage_height);scale=$layout.scale}
        foreach ($result in $pass.results) {
            $groups = @{}
            foreach ($variant in $viewNames) {
                $groups[$variant] = @{language=$result.language;preprocessing=$variant;lines=@();words=@();seconds=$result.seconds}
            }
            foreach ($line in $result.lines) {
                $segments = @{}
                foreach ($word in $line.words) {
                    for ($tileIndex=0;$tileIndex -lt $layout.tiles.Length;$tileIndex++) {
                        $tile=$layout.tiles[$tileIndex]; $b=$tile.montage_bbox
                        if ($word.x -ge $b[0]-0.001 -and $word.y -ge $b[1]-0.001 -and
                            $word.x+$word.width -le $b[0]+$b[2]+0.001 -and $word.y+$word.height -le $b[1]+$b[3]+0.001) {
                            $src=$tile.source_bbox; $sx=$src[2]/[double]$b[2]; $sy=$src[3]/[double]$b[3]
                            $mapped = [ordered]@{text=$word.text;language=$result.language;
                                x=$src[0]+($word.x-$b[0])*$sx;y=$src[1]+($word.y-$b[1])*$sy;
                                width=$word.width*$sx;height=$word.height*$sy;roi_index=$tile.roi_index}
                            if (!$segments.ContainsKey($tileIndex)) { $segments[$tileIndex]=@() }
                            $segments[$tileIndex] += $mapped
                            break
                        }
                    }
                }
                foreach ($tileIndex in @($segments.Keys | Sort-Object)) {
                    $tile=$layout.tiles[$tileIndex]; $words=@($segments[$tileIndex])
                    $left=($words | ForEach-Object {$_.x} | Measure-Object -Minimum).Minimum
                    $top=($words | ForEach-Object {$_.y} | Measure-Object -Minimum).Minimum
                    $right=($words | ForEach-Object {$_.x+$_.width} | Measure-Object -Maximum).Maximum
                    $bottom=($words | ForEach-Object {$_.y+$_.height} | Measure-Object -Maximum).Maximum
                    # Preserve real OCR spacing only when the line belongs to
                    # one tile. Cross-tile lines cannot prove nickname spacing.
                    $text = if ($segments.Count -eq 1 -and $words.Count -eq @($line.words).Count) { $line.text } else { '' }
                    $mappedLine = @{text=$text;words=$words;roi_index=$tile.roi_index;
                        bounding_box=@{x=$left;y=$top;width=$right-$left;height=$bottom-$top}}
                    $groups[$tile.preprocessing].lines += $mappedLine
                    $groups[$tile.preprocessing].words += $words
                }
            }
            foreach ($variant in $viewNames) {
                $group=$groups[$variant]; $group.line_count=@($group.lines).Count; $group.word_count=@($group.words).Count
                $mappedResults += $group; $allWords += $group.words
            }
        }
        }
        $response.results=$mappedResults; $response.words=$allWords
        $response.seconds=$totalSeconds; $response.ocr_montages=$montages
        $response.image_path=$path; $response.image_width=$layout.image_width; $response.image_height=$layout.image_height
        $response.ocr_method='calibrated_name_regions'
        $response.preprocessing=@('colour_3x','light_text_140_3x','light_text_120_3x','soft_text_185_3x')
        $response.region_scale=$firstLayout.scale
        $response.region_layout=$allTiles
        $response.regions=$regions
        $response.montage_dimensions=@($firstLayout.montage_width,$firstLayout.montage_height)
        $auxiliaryTimer = [Diagnostics.Stopwatch]::StartNew()
        $response.auxiliary_names = @(Read-AuxiliaryNames $path $regions)
        $response.auxiliary_status = if ($script:nameFrameError) { 'frame_error' } else { $script:nameRecognizerStatus }
        $response.auxiliary_seconds = [Math]::Round($auxiliaryTimer.Elapsed.TotalSeconds,4)
        $response.seconds += $response.auxiliary_seconds
        return $response
    } finally {
        # This is a worker-created intermediate only. Keep the source PNG for UI.
        if ([IO.File]::Exists($montagePath)) { [IO.File]::Delete($montagePath) }
    }
}

while ($null -ne ($inputLine = [Console]::In.ReadLine())) {
    $request = $null
    $started = [Diagnostics.Stopwatch]::StartNew()
    try {
        if ($inputLine.Length -gt 16384) { throw 'OCR request exceeds the protocol size limit.' }
        $request = $inputLine | ConvertFrom-Json
        $languages = @($request.languages)
        if ($languages.Count -lt 1 -or $languages.Count -gt 4) { throw 'One to four OCR languages are required.' }
        foreach ($tag in $languages) { if ($tag -notmatch '^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$') { throw 'Invalid OCR language tag.' } }
        $capture = $null
        if ($request.op -eq 'warmup') {
            $response = Initialize-OCRLanguages $languages
            Initialize-NameRecognizer
            $response.auxiliary_status = $script:nameRecognizerStatus
            $response.operation = 'warmup'
            $response.results = @(); $response.words = @()
        } elseif ($request.op -eq 'capture') {
            $path = Join-Path $TempRoot ('game-' + [Guid]::NewGuid().ToString('N') + '.png')
            $capture = [BAScoutOCR.NativeCapture]::Capture($path, [bool]$request.require_foreground, $ClientPid)
            $capturedAt = [DateTime]::UtcNow.ToString('o')
            if (!$capture.ok) {
                Send-Json @{id=$request.id;ok=$false;status=$capture.status;error=$capture.error;results=@();words=@();seconds=$started.Elapsed.TotalSeconds}
                continue
            }
            $captures.Enqueue($path)
            # Only delete files created by this worker, never the caller's image.
            while ($captures.Count -gt 8) { [IO.File]::Delete($captures.Dequeue()) }
            if ($request.capture_only) {
                $response = @{image_path=$path;image_width=$capture.image_width;image_height=$capture.image_height;results=@();words=@();languages=@();available_languages=$availableLanguages;coordinate_system='original_image_pixels';confidence_available=$false}
            } elseif ($null -ne $request.regions) { $response = Read-RegionsOCR $path $languages $request.regions }
            else { $response = Read-OCR $path $languages }
            $response.capture = $capture
            $response.capture_method = 'PrintWindow_client_only'
            $response.captured_at = $capturedAt
            $response.frame_freshness_verified = $false
        } elseif ($request.op -eq 'extract') {
            if ($null -ne $request.regions) { $response = Read-RegionsOCR $request.image_path $languages $request.regions }
            else { $response = Read-OCR $request.image_path $languages }
        }
        else { throw 'Unknown OCR operation.' }
        $response.id=$request.id; $response.ok=$true; $response.status='ok'; $response.error=$null
        $response.completed_at=[DateTime]::UtcNow.ToString('o')
        $response.seconds=[Math]::Round($started.Elapsed.TotalSeconds,4)
        Send-Json $response
    } catch {
        Send-Json @{id=$request.id;ok=$false;status='worker_error';error=$_.Exception.Message;results=@();words=@();seconds=$started.Elapsed.TotalSeconds}
    }
}
