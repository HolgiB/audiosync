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
tags and the default track will be wrong:

    ./audiosync.py film-en.mkv film-de.mkv -o film.mkv \
        --ref-lang en --second-lang de \
        --ref-title English --second-title Deutsch \
        --default-lang de

## What V2 does

1. Checks the video stream parameters.
2. Decodes sample frames from both files and compares downscaled grayscale
   structural signatures at several positions. Recordings of the same release
   can start at slightly different times (different channel padding), so a
   constant video offset is first estimated and applied before comparing. The
   estimate is refined to sub-grid resolution (0.05 s), because the signature
   window is only a few frames wide and coarse seeking can otherwise push
   genuine matches below the similarity threshold.
3. Extracts both first audio tracks as low-rate mono PCM solely for analysis.
4. Determines the relative audio offset at several positions throughout the
   movie (default 5).
5. Uses the median offset and rejects measurements that disagree by more than
   150 ms.
6. Positive offset: trims the leading audio of the second recording with
   `atrim` and resets its PTS with `asetpts=PTS-STARTPTS`.
7. Negative offset: delays the second recording's audio with `adelay` and
   resets its PTS with `asetpts=PTS-STARTPTS`.
8. Muxes the reference video + both audio tracks + the subtitle tracks of both
   inputs into the final MKV.

## Parameters

    positional:
      reference            reference recording, e.g. German
      second               second recording, e.g. English

    -o, --output           output file (required)
        --ref-lang         language tag of the reference track (default: de)
        --second-lang      language tag of the second track (default: en)
        --ref-title        title of the reference track (default: Deutsch)
        --second-title     title of the second track (default: English)
        --default-lang     which audio track is flagged as default: de or en
                           (default: de) — use this whenever the roles are
                           swapped so the German track stays the default one
        --max-offset       maximum search window in seconds for the audio
                           correlation (default: 180)
        --samples          number of audio analysis positions (default: 5)
        --threshold        minimum normalized correlation score for an audio
                           measurement to be accepted (default: 0.08)
        --force            continue despite a failed video identity check
        --dry-run          run all checks and analysis, but write no output

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

The corrected second audio is encoded as AAC 192 kbit/s. This is intentional:
once an arbitrary audio filter (`adelay`/`atrim`) is applied, reliable
stream-copying is not possible for arbitrary source codecs. The original
reference audio is also currently encoded by ffmpeg because both mapped audio
streams share the same `-c:a aac` setting.

If lossless audio preservation is important, the mux step can be extended to
encode only the corrected second track while stream-copying the reference
track.

## Dry run

    ./audiosync.py film-de.mkv film-en.mkv -o film.mkv --dry-run

## Override video check

Only use this if you have independently verified that the releases are the
same:

    ./audiosync.py film-de.mkv film-en.mkv -o film.mkv --force

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
