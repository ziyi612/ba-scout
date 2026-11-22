namespace BAScoutOCR {
  using System;
  using System.Drawing;
  using System.Runtime.InteropServices;
  using System.Text;

  // Windows SDK ITensorNative, windows.ai.machinelearning.native.h.
  [ComImport, Guid("52F547EF-5B03-49B5-82D6-565F1EE0DD49"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  internal interface ITensorNative {
    void GetBuffer(out IntPtr data, out uint byteCount);
  }

  [ComImport, Guid("C71E953F-37B4-4564-8658-D8396866DB0D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  internal interface ISessionOptionsNative {
    void SetIntraOpNumThreadsOverride(uint count);
  }

  public sealed class NameInput {
    public float[] pixels;
    public int width;
    public bool clipped;
  }

  public sealed class NameReading {
    public string text;
    public double confidence;
  }

  public static class NameRecognizer {
    [ThreadStatic] static float[] scores;

    public static void UseOneCpuThread(object options) {
      ((ISessionOptionsNative)options).SetIntraOpNumThreadsOverride(1);
    }

    public static NameInput Prepare(Bitmap frame, double[] box) {
      int left=(int)Math.Round(box[0]*frame.Width), top=(int)Math.Round(box[1]*frame.Height);
      int right=Math.Min(frame.Width,left+(int)Math.Round(box[2]*frame.Width));
      int bottom=Math.Min(frame.Height,top+(int)Math.Round(box[3]*frame.Height)+2);
      left=Math.Max(0,left-2);
      top=Math.Max(0,top-2);
      int height=bottom-top, width=right-left;
      if (height<=0 || width<=0 || (long)width*height>1024000) return null;
      byte[] gray=new byte[height*width];
      int lastInk=-1, edgeInk=0;
      for (int y=0;y<height;y++) {
        for (int x=0;x<width;x++) {
          Color c=frame.GetPixel(left+x,top+y);
          int intensity=Math.Min(c.R,Math.Min(c.G,c.B));
          if (intensity>180) {
            lastInk=Math.Max(lastInk,x);
            if (x>=width-2 && y>=2 && y<height-2) edgeInk++;
          }
          gray[y*width+x]=(byte)Math.Min(255,Math.Max(0,(int)((intensity-112)*2.1)));
        }
      }
      if (lastInk<0) return null;
      int cropped=Math.Min(width,lastInk+3);
      int destWidth=(int)Math.Ceiling(48.0*cropped/height);
      if (destWidth<8 || destWidth>1024) return null;
      int plane=48*destWidth;
      float[] pixels=new float[3*plane];
      for (int y=0;y<48;y++) {
        double sy=Math.Max(0,Math.Min(height-1,(y+.5)*height/48-.5));
        int y0=(int)sy, y1=Math.Min(height-1,y0+1);
        for (int x=0;x<destWidth;x++) {
          double sx=Math.Max(0,Math.Min(cropped-1,(x+.5)*cropped/destWidth-.5));
          int x0=(int)sx, x1=Math.Min(cropped-1,x0+1);
          double a=gray[y0*width+x0]*(1-sx+x0)+gray[y0*width+x1]*(sx-x0);
          double b=gray[y1*width+x0]*(1-sx+x0)+gray[y1*width+x1]*(sx-x0);
          pixels[y*destWidth+x]=(float)(Math.Round(a*(1-sy+y0)+b*(sy-y0))/127.5-1);
        }
      }
      Array.Copy(pixels,0,pixels,plane,plane);
      Array.Copy(pixels,0,pixels,2*plane,plane);
      return new NameInput { pixels=pixels, width=destWidth, clipped=edgeInk>0 };
    }

    public static NameReading Decode(object tensor, int steps, string[] chars) {
      if (steps<=0 || steps>160 || chars.Length<3 || chars.Length>10000)
        throw new InvalidOperationException("Unexpected recognizer output shape.");
      IntPtr ptr; uint bytes;
      ((ITensorNative)tensor).GetBuffer(out ptr,out bytes);
      int count=checked(steps*chars.Length);
      if (ptr==IntPtr.Zero || bytes<count*4) throw new InvalidOperationException("Incomplete recognizer output.");
      if (scores==null || scores.Length<count) scores=new float[count];
      Marshal.Copy(ptr,scores,0,count);
      GC.KeepAlive(tensor);
      var text=new StringBuilder();
      double sum=0;int tokens=0, last=-1;
      for (int t=0;t<steps;t++) {
        int offset=t*chars.Length, best=0;
        for (int k=1;k<chars.Length;k++)
          if (scores[offset+k]>scores[offset+best]) best=k;
        if (best!=0 && best!=last) {
          text.Append(chars[best]); sum+=scores[offset+best]; tokens++;
        }
        last=best;
      }
      return new NameReading { text=text.ToString().Trim(), confidence=tokens>0?sum/tokens:0 };
    }
  }
}
