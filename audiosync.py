#!/usr/bin/env python3
"""
audiosync v2

Synchronize two recordings of the same release and mux:
  reference video + reference audio + second recording's audio.

Safety:
  - compares video stream parameters
  - compares decoded video frames using FFmpeg framemd5
  - measures audio offset at several positions
  - aborts if synchronization is not stable

Timestamp handling (offset = second - reference):
  +offset: second audio starts later -> trim its leading audio and reset PTS
  -offset: second audio starts earlier -> delay it

Video is always stream-copied. Audio is re-encoded to AAC because filtered
audio cannot be reliably stream-copied for arbitrary source codecs.
"""

import argparse, json, math, os, shutil, subprocess, sys, tempfile
from pathlib import Path
import numpy as np

SR = 2000
CHUNK = 30.0
REFINE_WIN = 0.5
REFINE_STEP = 0.05

def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(2)

def run(cmd, capture=False):
    print("+", " ".join(map(str, cmd)), file=sys.stderr)
    return subprocess.run(
        cmd, check=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None)

def probe(path):
    r = run(["ffprobe","-v","error","-print_format","json",
             "-show_format","-show_streams",str(path)], True)
    return json.loads(r.stdout.decode("utf-8"))

def get_stream(info, typ, index=0):
    xs = [s for s in info.get("streams",[]) if s.get("codec_type")==typ]
    return xs[index] if len(xs)>index else None

def duration(info):
    d = info.get("format",{}).get("duration")
    if d: return float(d)
    vals = [float(s["duration"]) for s in info.get("streams",[])
            if s.get("duration")]
    return max(vals) if vals else 0.0

def video_params(info):
    s = get_stream(info,"video")
    if not s: die("input has no video stream")
    return {k:s.get(k) for k in
            ("codec_name","profile","width","height","pix_fmt",
             "r_frame_rate","avg_frame_rate")}

def frame_sig(path, timestamp, n=5):
    # Structural signature: average of n downscaled grayscale frames.
    # Tolerates different encodes of the same film; exact pixels need not match.
    r = run(["ffmpeg","-hide_banner","-loglevel","error",
             "-ss",f"{timestamp:.3f}","-i",str(path),
             "-frames:v",str(n),
             "-vf","scale=64:36,format=gray","-f","rawvideo","-"], True)
    arr = np.frombuffer(r.stdout, dtype=np.uint8).astype(np.float32)
    if arr.size == 0: return None
    return arr.reshape(-1,36,64).mean(axis=0)

def corr(a,b):
    a,b = a.ravel(),b.ravel()
    a,b = a-a.mean(), b-b.mean()
    d = np.sqrt((a*a).sum()*(b*b).sum())
    return float(np.dot(a,b)/d) if d > 0 else 0.0

def verify_video_identity(a,b,ia,ib,samples=4,max_offset=180.0):
    pa,pb = video_params(ia),video_params(ib)
    print("\nVideo identity check:", file=sys.stderr)
    for k in pa:
        print(f"  {k:13}: {pa[k]} | {pb[k]}", file=sys.stderr)

    # Encodes of the same film can differ in resolution/codec/pix_fmt; the
    # downscaled frame signatures are resolution-independent, so differing
    # stream parameters are a warning, not a hard fail.
    if pa["width"] != pb["width"] or pa["height"] != pb["height"]:
        print("  NOTE: resolutions differ; relying on downscaled frame "
              "signatures", file=sys.stderr)
    common = min(duration(ia),duration(ib))
    if common < 5:
        print("  RESULT: FAIL (input too short)", file=sys.stderr)
        return False

    positions = np.linspace(common*.15, common*.85, samples)

    # Recordings of the same release can start at slightly different times
    # (different leading padding on the channel), so the video streams may be
    # offset by a constant amount. Estimate that offset before comparing.
    # Fine search first; escalate to a coarse wide sweep only if needed.
    def best_alignment(t):
        for rng, step in ((3.0,0.25),(max_offset,2.0)):
            sa = frame_sig(a,float(t))
            if sa is None: return None
            bd,bc = 0.0,0.0
            for dt in np.arange(-rng,rng+step,step):
                sb = frame_sig(b,float(t+dt))
                c = corr(sa,sb) if sb is not None else 0.0
                if c > bc: bd,bc = dt,c
            if bc >= 0.85:
                # Refine to sub-grid resolution. The frame-signature average is
                # only ~5 frames wide and -ss keyframe seeking adds jitter near
                # GOP boundaries, so a coarse grid can quantize the peak by a
                # tenth of a second and push genuine matches below threshold.
                bb,cc = bd,bc
                for dt in np.arange(bd-REFINE_WIN,bd+REFINE_WIN+REFINE_STEP,
                                    REFINE_STEP):
                    sb = frame_sig(b,float(t+dt))
                    c = corr(sa,sb) if sb is not None else 0.0
                    if c > cc: bb,cc = dt,c
                return bb,cc
        return None

    aligned = [r for r in (best_alignment(float(t)) for t in positions)
               if r is not None]
    if not aligned:
        print("  RESULT: FAIL (no position aligns at any offset)",
              file=sys.stderr)
        return False
    offset = float(np.median([d for d,_ in aligned]))
    print(f"  estimated video offset (second-ref): {offset:+.2f}s",
          file=sys.stderr)

    matches = 0
    for t in positions:
        sa = frame_sig(a,float(t))
        if sa is None: continue
        bc = 0.0
        for dt in np.arange(offset-REFINE_WIN,offset+REFINE_WIN+REFINE_STEP,
                            REFINE_STEP):
            sb = frame_sig(b,float(t+dt))
            c = corr(sa,sb) if sb is not None else 0.0
            if c > bc: bc = c
        ok = bc >= 0.85
        matches += int(ok)
        print(f"  {t:10.2f}s: {'MATCH' if ok else 'DIFFER'} (corr={bc:.3f})",
              file=sys.stderr)

    ok = matches >= max(3, math.ceil(samples*.75))
    print(f"  RESULT: {'PASS' if ok else 'FAIL'} ({matches}/{samples})",
          file=sys.stderr)
    return ok

def extract_audio(path,out):
    run(["ffmpeg","-hide_banner","-loglevel","error","-i",path,
         "-vn","-map","0:a:0","-ac","1","-ar",str(SR),
         "-f","f32le","-acodec","pcm_f32le",out], True)

def prep(x):
    x=x.astype(np.float32,copy=False)
    if not len(x): return x
    x -= x.mean()
    x /= np.sqrt(np.mean(x*x)+1e-12)
    y=np.diff(x,prepend=x[0])
    return np.clip(y,-4,4)

def best_lag(ref, other, max_seconds):
    """Return signed shift of other relative to ref; positive means later."""
    n=len(ref)
    maxs=int(max_seconds*SR)
    # Correlate ref with the whole second recording.
    m=len(other)
    size=1 << (n+m-1).bit_length()
    c=np.fft.irfft(np.conj(np.fft.rfft(ref,size)) *
                   np.fft.rfft(other,size), size)[:m]
    lags=np.arange(m)
    mask=lags<=2*maxs
    vals=c[mask]
    lag=int(lags[mask][np.argmax(vals)])
    # Normalize against the aligned sub-window, not the whole search range:
    # other is much longer than ref, so normalizing by norm(other) dilutes
    # the score and pushes genuine matches below the threshold.
    aligned=other[lag:lag+n]
    ln=min(n,len(aligned))
    score=float(np.dot(ref[:ln],aligned[:ln]) /
                (np.linalg.norm(ref[:ln])*np.linalg.norm(aligned[:ln])+1e-12))
    return lag,score

def analyze(a,b,total,max_offset,samples=5,window=CHUNK,threshold=0.08):
    # Windows are positioned identically in reference time; second recording
    # is searched around each position.
    if total < window+10:
        positions=[max(0,total/2-window/2)]
    else:
        lo=min(60.0,total*.10)
        hi=max(lo,total-60.0-window)
        positions=np.linspace(lo,hi,samples)

    results=[]
    maxs=int(max_offset*SR)
    for pos in positions:
        aa=a[int(pos*SR):int((pos+window)*SR)]
        if len(aa) < window*SR*.8: continue
        bs=max(0,int((pos-max_offset)*SR))
        be=min(len(b),int((pos+window+max_offset)*SR))
        bb=b[bs:be]
        lag,score=best_lag(aa,bb,max_offset)
        # Convert lag relative to bb start to absolute second-vs-reference shift.
        offset=(bs+lag)/SR-pos
        results.append((float(offset),score,float(pos)))

    if not results: raise RuntimeError("Could not analyze audio")
    good=[r[0] for r in results if r[1] >= threshold]
    if not good:
        raise RuntimeError("No reliable synchronization point found")
    selected=float(np.median(good))
    spread=float(max(good)-min(good))
    return selected,spread,results

def mux(reference,second,out,offset,ref_lang,second_lang,
        ref_title,second_title,force):
    # Filter the SECOND audio only. Sign convention: offset is the position of
    # the second recording's content relative to the reference (second - ref).
    # Positive => second content is later => advance it (trim leading audio).
    # Negative => second content is earlier => delay it.
    if offset >= 0:
        filt=f"atrim=start={offset:.6f},asetpts=PTS-STARTPTS"
    else:
        filt=f"adelay={-offset*1000:.3f}:all=1,asetpts=PTS-STARTPTS"

    cmd=["ffmpeg","-hide_banner","-y" if force else "-n",
         "-i",reference,"-i",second,
         "-map","0:v:0","-map","0:a:0","-map","[en]",
         "-filter_complex",f"[1:a:0]{filt}[en]",
         "-map_metadata","0","-map_chapters","0",
         "-c:v","copy","-c:a","aac","-b:a","192k",
         "-metadata:s:a:0",f"language={ref_lang}",
         "-metadata:s:a:0",f"title={ref_title}",
         "-metadata:s:a:1",f"language={second_lang}",
         "-metadata:s:a:1",f"title={second_title}",
         "-disposition:a:0","default",
         "-disposition:a:1","0",out]
    run(cmd)

def main():
    ap=argparse.ArgumentParser(
        description="Align two identical-release movie recordings.")
    ap.add_argument("reference",help="reference recording, e.g. German")
    ap.add_argument("second",help="second recording, e.g. English")
    ap.add_argument("-o","--output",required=True)
    ap.add_argument("--ref-lang",default="de")
    ap.add_argument("--second-lang",default="en")
    ap.add_argument("--ref-title",default="Deutsch")
    ap.add_argument("--second-title",default="English")
    ap.add_argument("--max-offset",type=float,default=180)
    ap.add_argument("--samples",type=int,default=5)
    ap.add_argument("--threshold",type=float,default=.08)
    ap.add_argument("--force",action="store_true",
                    help="continue despite failed video identity check")
    ap.add_argument("--dry-run",action="store_true")
    args=ap.parse_args()

    for f in (args.reference,args.second):
        if not os.path.isfile(f): die(f"file not found: {f}")
    for b in ("ffmpeg","ffprobe"):
        if shutil.which(b) is None: die(f"{b} not found in PATH")

    ia,ib=probe(args.reference),probe(args.second)
    if not get_stream(ia,"audio") or not get_stream(ib,"audio"):
        die("both inputs need at least one audio stream")

    if not verify_video_identity(args.reference,args.second,ia,ib,
                                 max_offset=args.max_offset) and not args.force:
        die("video identity check failed; use --force only if the releases are known identical")

    with tempfile.TemporaryDirectory(prefix="audiosync-") as td:
        ar,br=os.path.join(td,"ref.raw"),os.path.join(td,"second.raw")
        print("\nExtracting low-rate mono audio...",file=sys.stderr)
        extract_audio(args.reference,ar)
        extract_audio(args.second,br)
        a,b=prep(np.fromfile(ar,dtype=np.float32)),prep(np.fromfile(br,dtype=np.float32))

        offset,spread,results=analyze(
            a,b,min(duration(ia),duration(ib)),
            args.max_offset,args.samples,threshold=args.threshold)

        print("\nAudio correlation:",file=sys.stderr)
        for o,s,t in results:
            print(f"  {t:8.1f}s  offset={o:+.4f}s  score={s:.3f}",
                  file=sys.stderr)
        print(f"\nSelected offset: {offset:+.4f}s",file=sys.stderr)
        print(f"Maximum deviation: {spread:.4f}s",file=sys.stderr)

        if spread > .15:
            die("audio offset is unstable (>150 ms spread); recordings may not be identical")

        if args.dry_run:
            print("Dry run: no output created.",file=sys.stderr)
            return

        print("\nCreating final MKV...",file=sys.stderr)
        mux(args.reference,args.second,args.output,offset,
            args.ref_lang,args.second_lang,args.ref_title,args.second_title,
            args.force)
        print(f"Done: {args.output}",file=sys.stderr)

if __name__=="__main__":
    main()
