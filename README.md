# audiosync v2

Small Linux tool for two recordings of the **same movie release**, e.g. one
recorded with German audio and one with English audio. It aligns the audio of the
second recording to the reference and muxes video + both audio tracks into a
single MKV.

## Requirements

    sudo apt install ffmpeg python3-numpy

## Usage

    ./audiosync.py film-de.mkv film-en.mkv -o film.mkv

The first file is the reference. Its video and first audio track are retained.
The first audio track from the second file is added as the second language track.

If the *second* file carries the better video, swap the two arguments — but then
the language tags and the default flag must be passed explicitly, otherwise the
tags and the default track will be wrong (track titles, however, follow the
language tags automatically):

    ./audiosync.py film-en.mkv film-de.mkv -o film.mkv \
        --ref-lang en --second-lang de \
        --default-lang de

## What V2 does

1. Checks the video stream parameters.
2. Decodes sample frames from both files and compares downscaled grayscale
   structural signatures at several positions. Recordings of the same release
   can start at slightly different times (different channel padding), so a
   constant video offset is first estimated and applied before comparing. The
   signature preserves the source aspect ratio and pads to a common canvas, so
   a cropped transfer and a letterboxed 16:9 transfer of the same film compare
   correctly instead of failing on the differing frame geometry. The offset
   estimate is refined to sub-grid resolution (0.05 s), because the signature
   window is only a few frames wide and coarse seeking can otherwise push
   genuine matches below the similarity threshold.
3. Extracts both first audio tracks as low-rate mono PCM solely for analysis.
4. Determines the relative audio offset at several positions throughout the
   movie (default 5) by waveform cross-correlation. If the two language dubs
   share too little raw waveform (dialogue-heavy passages), the waveform pass
   cannot find enough reliable positions; the tool then retries on a
   differentiated log-RMS onset envelope. Music and effects keep their timing
   across dubs, so their onsets still align even where the speech does not.
5. Keeps the largest cluster of measurements that agree within 150 ms — an
   isolated outlier (quiet scene, a repeated music cue) whose score is still
   just above the threshold is dropped — uses the median of that cluster, and
   rejects the pair when the cluster itself still spreads by more than 150 ms.
   A genuine speed difference or a different cut shows up as a consistent
   trend across positions, not as an isolated point, so it still fails.
6. Positive offset: trims the leading audio of the second recording with
   `atrim` and resets its PTS with `asetpts=PTS-STARTPTS`.
7. Negative offset: delays the second recording's audio with `adelay` and
   resets its PTS with `asetpts=PTS-STARTPTS`.
8. Muxes the reference video + both audio tracks + the subtitle tracks of both
   inputs into the final MKV. Packets are interleaved strictly by DTS
   (`-max_interleave_delta 0`); with FFmpeg's default interleaving, files with
   two audio tracks can trigger "possibly bad interleaving" in
   MPlayer/SMPlayer, which then produces no sound when switching tracks.

## Parameters

    positional:
      reference            reference recording, e.g. German
      second               second recording, e.g. English

    -o, --output           output file (required)
        --ref-lang         language tag of the reference track (default: de)
        --second-lang      language tag of the second track (default: en)
        --ref-title        title of the reference track (default: derived from
                           --ref-lang, e.g. en -> English)
        --second-title     title of the second track (default: derived from
                           --second-lang, e.g. de -> Deutsch)
        --default-lang     which audio track carries the `default` flag: a
                           language code (de, en, fr, ...) or the words
                           `ref` / `second` (default: de). Use it whenever the
                           roles are swapped so the German track stays the
                           default one. A value that matches neither track's
                           language aborts with an error instead of silently
                           flagging the other track. Two- and three-letter
                           codes are treated as the same language, so
                           `--default-lang ger` also matches `--ref-lang de`.
        --tag-style        form of the language codes written to the metadata:
                           `asis` = pass through unchanged (default),
                           `iso639-1` = two letters (de), `iso639-2` = three
                           letters (ger). The track title is readable in every
                           case. Codes that are not recognised are reported on
                           stderr but still written unchanged.
        --audio            auto | copy | encode (default: auto). auto = a lossless
                           reference (FLAC/PCM/ALAC) is encoded to AAC, a lossy
                           one is stream-copied; copy = always copy the reference;
                           encode = always encode it. The second track is always
                           encoded (it runs through the offset filter).
        --aac-bitrate-stereo   bitrate for encoded tracks with up to two channels
                           (default: 192k)
        --aac-bitrate-multi    bitrate for more than two channels, e.g. 5.1
                           (default: 384k)
        --max-offset       maximum search window in seconds for the audio
                           correlation (default: 180)
        --samples          number of audio analysis positions (default: 5)
        --threshold        minimum normalized correlation score for a waveform
                           measurement to be accepted (default: 0.08). The
                           envelope fallback uses its own fixed threshold.
        --offset           manual offset in seconds (second - reference); skips
                           the audio correlation and the spread check. Use it
                           when both mixes share no usable signal (different
                           dubs/masters) but the offset is known — measure it
                           on the decoded frames, not by guessing.
        --force            continue despite a failed video identity check
        --dry-run          run all checks and analysis, but write no output

### Language tags

`--ref-lang`, `--second-lang` and `--default-lang` accept the two-letter
(ISO 639-1) and the three-letter form (`ger`, `eng`, `fre`, ...) alike. Which
form ends up in the file is controlled by `--tag-style`; the track title is
derived from the same table and is always a readable name:

    de/ger Deutsch          en/eng English            fr/fre Français
    es/spa Español          it/ita Italiano           pt/por Português
    nl/dut Nederlands       pl/pol Polski             ru/rus Русский
    uk/ukr Українська       sv/swe Svenska            da/dan Dansk
    no/nor Norsk            fi/fin Suomi              is/ice Íslenska
    cs/cze Čeština          sk/slo Slovenčina         hu/hun Magyar
    ro/rum Română           bg/bul Български          el/gre Ελληνικά
    tr/tur Türkçe           he/heb עברית              ar/ara العربية
    fa/per فارسی            hi/hin हिन्दी               th/tha ไทย
    vi/vie Tiếng Việt       id/ind Bahasa Indonesia   ja/jpn 日本語
    ko/kor 한국어             zh/chi 中文                ca/cat Català
    hr/hrv Hrvatski         sr/srp Српски             lt/lit Lietuvių
    lv/lav Latviešu         et/est Eesti

Anything outside this table is written through as given — a warning on stderr
names the known codes so a typo does not disappear into the metadata silently.

## Which file supplies the video?

The reference video is copied with `-c:v copy`, so there is **no video
re-encoding** — and the output video is exactly the reference's video. The
quality of the result therefore depends entirely on which file you pass first.

Before merging, compare both sources:

    ffprobe -v error -select_streams v:0 \
        -show_entries stream=codec_name,width,height \
        -show_entries format=duration,size -of json FILE

If the second file has the higher resolution/bitrate, swap the arguments and set
`--ref-lang`, `--second-lang`, `--ref-title`, `--second-title` and
`--default-lang` accordingly (see Usage above).

## Subtitles

The subtitle tracks of **both** inputs are muxed into the output
(`-map 0:s? -map 1:s? -c:s copy`), reference subtitles first. Nothing is
re-encoded.

Known limitation: the tool does not set the subtitle disposition explicitly, so
the flags are whatever the muxer derives — in practice the tracks end up with
`default=1`, and a source-side `forced` flag is not reliably preserved. For
forced subtitles (foreign-language dialogue only) fix the flags afterwards:

    mkvpropedit film_dual.mkv \
        --edit track:s1 --set flag-forced=1 --set flag-default=0 \
        --edit track:s2 --set flag-forced=0 --set flag-default=0

Always verify with `ffprobe -show_streams` and look at `disposition`.

## Audio encoding

The **reference audio is stream-copied** (`-c:a:0 copy`): it keeps its original
codec and bitrate bit-for-bit, with no generation loss. This matters because the
reference track is usually the one the library plays by default.

Which track is copied and which is encoded follows `--audio`:

- **Reference track:** a lossy source (MP3/AAC/AC3/...) is **stream-copied**, so
  its existing bitrate is kept bit-for-bit and no further generation is added. A
  **lossless** source (FLAC/PCM/ALAC) is **encoded to AAC** — otherwise the output
  drags the lossless track along into the library (5.1 FLAC runs at ~4 Mbit/s).
- **Second track:** always encoded. It runs through the offset filter
  (`adelay`/`atrim`), and a filtered stream cannot be stream-copied.

The AAC encoder prepends 1024 samples of priming (21.3 ms at 48 kHz). FFmpeg's
Matroska path does not reliably trim them on playback, so every encoded track
would otherwise land ~21 ms late. The mux step compensates this before applying
the measured offset, for both an encoded reference track and the always-encoded
second track.

Bitrates are channel-aware: **192k** up to two channels, **384k** for more (5.1).
The stereo figure is not valid for six channels — encoding 5.1 at a stereo rate,
or down to stereo, would be a second downmix rather than a transcode.

192k follows from the measurements below: with a lossless capture the transcode is
the only lossy step, nothing masks it any more, and 192k buys 2.1 dB over 160k for
28 MB per 2 h track.

160k is a measured floor, not a guess. Test signal: a lossless music excerpt,
first encoded to MP3 128k (the capture stage in this collection), then the AAC
transcode, comparing the error against the original:

| stage | total error | added by the AAC stage |
|---|---|---|
| MP3 128k (source) | -20.0 dB | — |
| + AAC 128k | -15.9 dB | -18.1 dB |
| + AAC 160k | -17.6 dB | -21.5 dB |
| + AAC 192k | -18.2 dB | -23.6 dB |

At 160k and 192k the added error sits *below* the error the source already
carries, so it is masked in practice — and the two are only 0.6 dB apart. At
128k the add-on equals the source's own error and the total error energy grows
~2.6x. So for a *lossy* source everything from 160k up is masked, and 128k is
where the second lossy step stops hiding — that was the argument for 160k. With a
*lossless* source the 192k/384k default applies instead, because nothing masks
the transcode any more. (RMS error, not a hearing model — valid as a
comparison between the three rates, not as an audibility proof.)

Result: two tracks of possibly different codecs (e.g. MP3 + AAC) in one MKV.
That is fine for Matroska and for every player used here.

Fully lossless (both tracks copied, the offset applied as a container-level
track shift via `mkvmerge --sync`) is possible but would mean replacing the mux
step; millisecond precision there is far below the ~20 ms A/V tolerance, so it
is a real option if it is ever worth the rework.

## Dry run

    ./audiosync.py film-de.mkv film-en.mkv -o film.mkv --dry-run

## Override video check

Only use this if you have independently verified that the releases are the
same:

    ./audiosync.py film-de.mkv film-en.mkv -o film.mkv --force

## Manual offset when the audio correlation fails

Separate dubs/masters sometimes share almost no waveform, and then even the
envelope fallback can end with `No reliable synchronization point found`.
Measure the offset **on the audio, never on the picture** — the two streams of a
capture can carry heads of different lengths. Measured case: picture aligned at
+4.70 s while the audio needed −5.28 s, i.e. the English capture's audio led its
own video by ~10 s.

    # second audio leads by 5.284 s -> offset = -5.2842
    ./audiosync.py film-de.mkv film-en.mkv -o film.mkv --offset -5.2842

Measurement: read both first audio tracks as 2 kHz mono, build the log-RMS onset
envelope (positive first difference, 20 ms hop) and scan ±15 s in 20 ms steps
over 5–8 windows spread across the film. Take the lag that wins at most
positions; a spread above ~50 ms means different cuts.

Calibrate the sign once with a synthetic pair: delay one track by a known 3.0 s
(`-af adelay=3000:all=1`) and check which sign the scan reports.

Always verify the finished file against itself: the two tracks must peak at
lag 0 (measured: 0.00 s at five positions). The picture offset is a cross-check
only — it answers "same cut", not "same timing".

## Offset meaning

`offset = second - reference`.

    +1.250s  second recording's audio starts 1.25 seconds later
             -> trim the first 1.25 seconds of the second track
    -1.250s  second recording's audio starts 1.25 seconds earlier
             -> delay the second track by 1.25 seconds

The tool deliberately aborts when the offset changes substantially during the
movie. That usually indicates different cuts, PAL/NTSC speed differences,
extra/missing scenes, or a bad correlation.

## Common pitfalls

**AVI/Xvid sources break the MKV mux.** An AVI whose video has no timestamps
fails with *"Can't write packet with unknown timestamp"* / *"Error muxing a
packet"* (exit 234). This is not a sync problem — remux first (stream copy, no
re-encoding) and use the remux as input:

    ffmpeg -fflags +genpts -i film-de.avi -c copy film-de.remux.mkv

**Reading the failure output**

    RESULT: FAIL (n/4) / no position aligns
        different transfer or cut — not mergeable

    audio offset is unstable (>150 ms spread)
        the video matches but the audio timing does not (different dub or mix)

    CalledProcessError from ffmpeg inside mux()
        container/timestamp issue, the analysis itself was fine (see AVI above)