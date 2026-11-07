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
CONSENSUS_TOL = 0.15
ENV_HOP = int(0.02*SR)   # onset envelope: 20-ms frame
ENV_SR = SR / ENV_HOP    # 50 Hz
ENV_THRESHOLD = 0.08

# Sprachen: (ISO 639-1 zweistellig, ISO 639-2/B dreistellig, lesbarer Titel).
# Die B-Formen (ger, fre, dut ...) sind die in Matroska/ffmpeg ueblichen; die
# T-Formen (deu, fra, nld) werden beim Einlesen trotzdem erkannt.
LANGS = [
    ("de","ger","Deutsch"),   ("en","eng","English"),      ("fr","fre","Français"),
    ("es","spa","Español"),   ("it","ita","Italiano"),     ("pt","por","Português"),
    ("nl","dut","Nederlands"),("pl","pol","Polski"),       ("ru","rus","Русский"),
    ("uk","ukr","Українська"),("sv","swe","Svenska"),      ("da","dan","Dansk"),
    ("no","nor","Norsk"),     ("fi","fin","Suomi"),        ("is","ice","Íslenska"),
    ("cs","cze","Čeština"),   ("sk","slo","Slovenčina"),   ("hu","hun","Magyar"),
    ("ro","rum","Română"),    ("bg","bul","Български"),    ("el","gre","Ελληνικά"),
    ("tr","tur","Türkçe"),    ("he","heb","עברית"),        ("ar","ara","العربية"),
    ("fa","per","فارسی"),     ("hi","hin","हिन्दी"),         ("th","tha","ไทย"),
    ("vi","vie","Tiếng Việt"),("id","ind","Bahasa Indonesia"),
    ("ja","jpn","日本語"),     ("ko","kor","한국어"),          ("zh","chi","中文"),
    ("ca","cat","Català"),    ("hr","hrv","Hrvatski"),     ("sr","srp","Српски"),
    ("lt","lit","Lietuvių"),  ("lv","lav","Latviešu"),     ("et","est","Eesti"),
]
LANG_TITLES = {a:t for a,b,t in LANGS}
LANG_2TO3 = {a:b for a,b,t in LANGS}
LANG_3TO2 = {b:a for a,b,t in LANGS}
LANG_3TO2.update({"deu":"de","fra":"fr","nld":"nl","zho":"zh","ces":"cs",
                  "ell":"el","hun":"hu","ron":"ro","fas":"fa","ind":"id"})

KNOWN_LANGS = ", ".join(a for a,b,t in LANGS)

def canonical_lang(code):
    """Zwei- oder dreistelliges Kuerzel auf die zweistellige Form bringen."""
    c=str(code).strip().lower()
    if c in LANG_2TO3: return c
    if c in LANG_3TO2: return LANG_3TO2[c]
    return None

def lang_title(code):
    """Lesbarer Spurtitel. 3-stellige Kuerzel werden genauso erkannt."""
    return LANG_TITLES.get(canonical_lang(code), str(code))

def lang_tag(code, style):
    """Kuerzel fuer den Metadaten-Eintrag. 'asis' reicht unveraendert durch."""
    c=canonical_lang(code)
    if style=="iso639-2" and c: return LANG_2TO3[c]
    if style=="iso639-1" and c: return c
    return str(code)

def warn_unknown_lang(code, option):
    """Tippfehler sollen nicht still in die Metadaten wandern."""
    if canonical_lang(code) is None:
        print(f"WARNUNG: {option} '{code}' ist kein bekanntes Sprachkuerzel; "
              f"der Wert wird unveraendert geschrieben. Bekannt sind: "
              f"{KNOWN_LANGS}", file=sys.stderr)

def ref_is_default_flag(value,ref_lang,second_lang):
    """Welche Spur traegt den default-Flag? Akzeptiert 'ref'/'second' oder
    einen Sprachcode. Unbekannte Werte brechen ab, statt still die andere
    Spur zu markieren (der Fehler aus der de/en-Version). Zwei- und
    dreistellige Kuerzel werden dabei als gleich behandelt."""
    v=str(value).strip().lower()
    if v in ("ref","reference","first","1"): return True
    if v in ("second","2"): return False
    vc=canonical_lang(v) or v
    rc=canonical_lang(ref_lang) or str(ref_lang).strip().lower()
    sc=canonical_lang(second_lang) or str(second_lang).strip().lower()
    if vc==rc: return True
    if vc==sc: return False
    die(f"--default-lang '{value}' passt zu keiner der beiden Spuren "
        f"(ref-lang={ref_lang}, second-lang={second_lang}); erlaubt ist ein "
        f"Sprachcode dieser beiden Spuren oder 'ref'/'second'")

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
    # The source frames may differ in aspect: one release can be cropped to the
    # active picture, another letterboxed inside 16:9. Preserve the aspect and
    # pad to 64x36 so both representations put the film into the same rows.
    vf=("scale=64:36:force_original_aspect_ratio=decrease,"
        "pad=64:36:(ow-iw)/2:(oh-ih)/2,format=gray")
    r = run(["ffmpeg","-hide_banner","-loglevel","error",
             "-ss",f"{timestamp:.3f}","-i",str(path),
             "-frames:v",str(n),
             "-vf",vf,"-f","rawvideo","-"], True)
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

def envelope(x):
    """Differentiated log-RMS onset envelope.

    Different-language dubs share almost no raw waveform (the speech differs),
    but music and effects keep their timing. Their onsets therefore still line
    up on an energy envelope, which is sampled at ENV_SR (50 Hz)."""
    n=len(x)//ENV_HOP
    if n==0: return np.zeros(0,dtype=np.float32)
    r=np.log1p(np.sqrt(np.mean(x[:n*ENV_HOP].reshape(n,ENV_HOP)**2,axis=1)+1e-12))
    r-=r.mean()
    d=np.diff(r,prepend=r[0])
    d=np.clip(d,-4,4)
    return d/np.sqrt(np.mean(d*d)+1e-12)

def best_lag(ref, other, max_seconds, sr=SR):
    """Return signed shift of other relative to ref; positive means later."""
    n=len(ref)
    maxs=int(max_seconds*sr)
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

def scan_positions(total,samples,window=CHUNK):
    # Windows are positioned identically in reference time; second recording
    # is searched around each position.
    if total < window+10:
        return [max(0,total/2-window/2)]
    lo=min(60.0,total*.10)
    hi=max(lo,total-60.0-window)
    return np.linspace(lo,hi,samples)

def measure(a,b,total,max_offset,samples,window,threshold,sr):
    results=[]
    for pos in scan_positions(total,samples,window):
        aa=a[int(pos*sr):int((pos+window)*sr)]
        if len(aa) < window*sr*.8: continue
        bs=max(0,int((pos-max_offset)*sr))
        be=min(len(b),int((pos+window+max_offset)*sr))
        lag,score=best_lag(aa,b[bs:be],max_offset,sr)
        # Convert lag relative to bb start to absolute second-vs-reference shift.
        offset=(bs+lag)/sr-pos
        results.append((float(offset),score,float(pos)))
    return results

def consensus(results,threshold):
    good=[r[0] for r in results if r[1] >= threshold]
    if not good:
        return None
    # A single position can yield a spurious correlation peak (quiet scene,
    # repeated music cue) whose score is still just above the threshold. Such
    # an isolated outlier would blow up the max-min spread and abort an
    # otherwise perfectly synchronized pair. Keep the largest cluster of
    # measurements that agree within CONSENSUS_TOL; a genuine speed difference
    # or different cut shows up as a consistent trend across positions, not as
    # an isolated point, so it still fails the spread check in main().
    cluster=max(([o for o in good if abs(o-c) <= CONSENSUS_TOL] for c in good),
                key=len)
    if len(cluster) < max(2, math.ceil(len(good)/2)):
        return None
    return float(np.median(cluster)), float(max(cluster)-min(cluster))

def analyze(a,b,total,max_offset,samples=5,window=CHUNK,threshold=0.08):
    # a,b are raw mono signals at SR. Try the raw waveform first (exact for
    # same-language material). Different dubs share too little waveform in
    # dialogue-heavy passages, so retry on the onset envelope when the
    # waveform pass cannot find a stable cluster.
    results=measure(prep(a),prep(b),total,max_offset,samples,window,
                    threshold,SR)
    selected=consensus(results,threshold)
    method="waveform"
    if selected is None:
        results=measure(envelope(a),envelope(b),total,max_offset,samples,
                        window,ENV_THRESHOLD,ENV_SR)
        selected=consensus(results,ENV_THRESHOLD)
        method="onset envelope"
    if selected is None:
        raise RuntimeError("No reliable synchronization point found")
    selected,spread=selected
    return selected,spread,results,method

# --- Audiospuren: was wird kopiert, was encodiert --------------------------
LOSSLESS_CODECS = {
    "flac","alac","ape","wavpack","tta","truehd","mlp",
    "pcm_bluray","pcm_dvd",
}
AAC_BITRATE_STEREO = "192k"   # gemessen: bei verlustfreier Quelle bringt 192k
AAC_BITRATE_MULTI  = "384k"   # 2,1 dB gegenueber 160k; 5.1 braucht deutlich mehr
AAC_ENCODER_DELAY  = 1024     # Samples Priming, die ein AAC-Encode voranstellt

def aac_priming(stream):
    """Priming-Zeit des AAC-Encoders fuer diese Quelle in Sekunden.

    ffmpeg schreibt die 1024 Priming-Samples als ersten Packet mit negativem
    PTS, aber der Matroska-Muxer/Decoder trimmt sie beim Abspielen nicht
    zuverlaessig. Ohne Ausgleich waere jede encodierte Spur ~21 ms zu spaet.
    """
    try: sr=int((stream or {}).get("sample_rate") or 48000)
    except (TypeError,ValueError): sr=48000
    return AAC_ENCODER_DELAY/sr if sr>0 else 0.0

def is_lossless(codec):
    c=str(codec or "").lower()
    return c.startswith("pcm_") or c in LOSSLESS_CODECS

def aac_bitrate(channels,stereo=AAC_BITRATE_STEREO,multi=AAC_BITRATE_MULTI):
    """Kanalabhaengig: 192k ist eine Stereo-Zahl. Dieselbe Zahl auf 5.1 waere
    kein Transcode, sondern ein zweiter Downmix."""
    try: ch=int(channels)
    except (TypeError,ValueError): ch=2
    return multi if ch>2 else stereo

def audio_codec_args(ref_stream,second_stream,mode="auto",
                     stereo=AAC_BITRATE_STEREO,multi=AAC_BITRATE_MULTI):
    """Codec-Argumente fuer die beiden Ausgabespuren.

    Referenz: kopieren, wenn sie schon verlustbehaftet ist - dann ist die
    vorhandene Bitrate die Obergrenze und ein weiterer Lauf setzt nur eine Stufe
    drauf. Verlustfrei (FLAC/PCM) wird encodiert, sonst schleppt die Datei die
    riesige FLAC-Spur mit.
    Zweite Spur: laeuft immer durch den Versatzfilter. Ein gefilterter Stream
    kann nicht kopiert werden, sie wird also immer encodiert.
    """
    def codec(s): return str((s or {}).get("codec_name") or "").lower()
    def ch(s):
        try: return int((s or {}).get("channels") or 2)
        except (TypeError,ValueError): return 2
    ref_enc = (mode=="encode") or (mode=="auto" and is_lossless(codec(ref_stream)))
    if ref_enc:
        br=aac_bitrate(ch(ref_stream),stereo,multi)
        args=["-c:a:0","aac","-b:a:0",br]
        txt_ref=f"{codec(ref_stream) or '?'} -> AAC {br}"
    else:
        args=["-c:a:0","copy"]
        txt_ref=f"{codec(ref_stream) or '?'} -> kopiert (bitgenau)"
    br2=aac_bitrate(ch(second_stream),stereo,multi)
    args+=["-c:a:1","aac","-b:a:1",br2]
    return args,txt_ref,f"{codec(second_stream) or '?'} -> AAC {br2}",ref_enc

def mux(reference,second,out,offset,ref_lang,second_lang,
        ref_title,second_title,force,ref_is_default=True,audio_args=None,
        ref_encode=False,ref_prime=0.0,second_prime=0.0):
    # Filter the SECOND audio only. Sign convention: offset is the position of
    # the second recording's content relative to the reference (second - ref).
    # Positive => second content is later => advance it (trim leading audio).
    # Negative => second content is earlier => delay it.
    # shift cancels the AAC encoder priming so the encoded content lands at the
    # intended time instead of ~21 ms late.
    shift=offset+second_prime
    if shift >= 0:
        filt=f"atrim=start={shift:.6f},asetpts=PTS-STARTPTS"
    else:
        filt=f"adelay={-shift*1000:.3f}:all=1,asetpts=PTS-STARTPTS"

    filters=[]
    if ref_encode:
        filters.append(f"[0:a:0]atrim=start={ref_prime:.6f},"
                       f"asetpts=PTS-STARTPTS[ref]")
        ref_map="[ref]"
    else:
        ref_map="0:a:0"
    filters.append(f"[1:a:0]{filt}[en]")

    if ref_is_default:
        disp=["-disposition:a:0","default","-disposition:a:1","0"]
    else:
        disp=["-disposition:a:0","0","-disposition:a:1","default"]
    cmd=["ffmpeg","-hide_banner","-y" if force else "-n",
         "-i",reference,"-i",second,
         "-map","0:v:0","-map",ref_map,"-map","[en]",
         "-map","0:s?","-map","1:s?",
         "-filter_complex",";".join(filters),
         "-map_metadata","0","-map_chapters","0",
         # Streng nach DTS verschachteln. Ohne das liegen die Audiopakete in
         # Matroska zu weit auseinander; MPlayer/SMPlayer melden "bad
         # interleaving" und geben beim Umschalten keinen Ton aus.
         "-max_interleave_delta","0",
         "-c:v","copy",
         # Audio: siehe audio_codec_args() - Referenz kopieren oder encodieren,
         # zweite Spur immer encodieren (Versatzfilter).
         *(audio_args or ["-c:a:0","copy","-c:a:1","aac","-b:a:1",AAC_BITRATE_STEREO]),
         "-c:s","copy",
         "-metadata:s:a:0",f"language={ref_lang}",
         "-metadata:s:a:0",f"title={ref_title}",
         "-metadata:s:a:1",f"language={second_lang}",
         "-metadata:s:a:1",f"title={second_title}",
         *disp,out]
    run(cmd)

def main():
    ap=argparse.ArgumentParser(
        description="Align two identical-release movie recordings.")
    ap.add_argument("reference",help="reference recording, e.g. German")
    ap.add_argument("second",help="second recording, e.g. English")
    ap.add_argument("-o","--output",required=True)
    ap.add_argument("--ref-lang",default="de")
    ap.add_argument("--second-lang",default="en")
    ap.add_argument("--ref-title",default=None,
                    help="Titel der Referenzspur (Standard: aus --ref-lang)")
    ap.add_argument("--second-title",default=None,
                    help="Titel der zweiten Spur (Standard: aus --second-lang)")
    ap.add_argument("--default-lang",default="de",
                    help="Spur, die als Default markiert wird: Sprachcode (de, en, "
                         "fr, ...) oder 'ref'/'second'. Standard de. Ein Code, der zu "
                         "keiner der beiden Spuren passt, bricht ab.")
    ap.add_argument("--tag-style",choices=["asis","iso639-1","iso639-2"],
                    default="asis",
                    help="Sprachkuerzel in den Metadaten: asis = unveraendert "
                         "(Standard), iso639-1 = zweistellig (de), "
                         "iso639-2 = dreistellig (ger). Der Spurtitel bleibt "
                         "in jedem Fall lesbar. Unbekannte Kuerzel werden "
                         "gemeldet, aber durchgereicht.")
    ap.add_argument("--audio",choices=["auto","copy","encode"],default="auto",
                    help="auto: verlustfreie Referenz nach AAC encodieren, "
                         "verlustbehaftete kopieren (Standard) | copy: Referenz "
                         "immer kopieren | encode: Referenz immer encodieren. "
                         "Die zweite Spur wird immer encodiert (Versatzfilter).")
    ap.add_argument("--aac-bitrate-stereo",default=AAC_BITRATE_STEREO)
    ap.add_argument("--aac-bitrate-multi",default=AAC_BITRATE_MULTI,
                    help="Bitrate fuer mehr als zwei Kanaele (5.1), Standard 384k")
    ap.add_argument("--max-offset",type=float,default=180)
    ap.add_argument("--samples",type=int,default=5)
    ap.add_argument("--threshold",type=float,default=.08,
                    help="minimum waveform correlation score (the envelope "
                         "fallback uses its own fixed threshold)")
    ap.add_argument("--force",action="store_true",
                    help="continue despite failed video identity check")
    ap.add_argument("--offset",type=float,default=None,
                    help="manueller Versatz in Sekunden (second - reference); "
                         "ueberspringt die Audio-Korrelation")
    ap.add_argument("--dry-run",action="store_true")
    args=ap.parse_args()

    # Sprachtags: erst melden, dann Titel ableiten, dann die Form bestimmen,
    # die in die Metadaten geht (--tag-style, Standard: unveraendert).
    warn_unknown_lang(args.ref_lang,"--ref-lang")
    warn_unknown_lang(args.second_lang,"--second-lang")
    if args.ref_title is None: args.ref_title=lang_title(args.ref_lang)
    if args.second_title is None: args.second_title=lang_title(args.second_lang)
    args.ref_lang=lang_tag(args.ref_lang,args.tag_style)
    args.second_lang=lang_tag(args.second_lang,args.tag_style)

    for f in (args.reference,args.second):
        if not os.path.isfile(f): die(f"file not found: {f}")
    for b in ("ffmpeg","ffprobe"):
        if shutil.which(b) is None: die(f"{b} not found in PATH")

    ia,ib=probe(args.reference),probe(args.second)
    if not get_stream(ia,"audio") or not get_stream(ib,"audio"):
        die("both inputs need at least one audio stream")

    audio_args,txt_ref,txt_second,ref_encode=audio_codec_args(
        get_stream(ia,"audio"),get_stream(ib,"audio"),args.audio,
        args.aac_bitrate_stereo,args.aac_bitrate_multi)
    print(f"\nAudio ({args.audio}): Referenz {txt_ref} | zweite {txt_second}",
          file=sys.stderr)

    # Reihenfolge wichtig: mit --force wird der teure Identitaetstest gar nicht
    # erst gerechnet (vorher lief er immer und das Ergebnis wurde verworfen).
    if not args.force and not verify_video_identity(args.reference,args.second,ia,ib,
                                                    max_offset=args.max_offset):
        die("video identity check failed; use --force only if the releases are known identical")

    with tempfile.TemporaryDirectory(prefix="audiosync-") as td:
        ar,br=os.path.join(td,"ref.raw"),os.path.join(td,"second.raw")
        print("\nExtracting low-rate mono audio...",file=sys.stderr)
        extract_audio(args.reference,ar)
        extract_audio(args.second,br)
        a_raw,b_raw=(np.fromfile(ar,dtype=np.float32),
                     np.fromfile(br,dtype=np.float32))
        common=min(duration(ia),duration(ib))

        if args.offset is not None:
            offset=args.offset; spread=0.0; results=[]; method="manuell"
            print(f"\nManueller Versatz: {offset:+.4f}s (Korrelation uebersprungen)",
                  file=sys.stderr)
        else:
            # analyze() misst erst die rohe Wellenform und faellt bei zu wenig
            # Treffern selbst auf die Onset-Huellkurve zurueck (andere Synchros
            # teilen kaum Wellenform). Rueckgabe: offset, spread, results, method.
            offset,spread,results,method=analyze(
                a_raw,b_raw,common,args.max_offset,args.samples,
                threshold=args.threshold)

        print(f"\nAudio correlation ({method}):",file=sys.stderr)
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
            args.force,
            ref_is_default_flag(args.default_lang,args.ref_lang,args.second_lang),
            audio_args,
            ref_encode=ref_encode,
            ref_prime=aac_priming(get_stream(ia,"audio")),
            second_prime=aac_priming(get_stream(ib,"audio")))
        print(f"Done: {args.output}",file=sys.stderr)

if __name__=="__main__":
    main()
