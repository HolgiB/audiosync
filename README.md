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
   estimate is refined to sub-grid resolution (0.05 s), because the signature
   window is only a few frames wide and coarse seeking can otherwise push
   genuine matches below the similarity threshold.
3. Extracts both first audio tracks as low-rate mono PCM solely for analysis.
4. Determines the relative audio offset at several positions throughout the
   movie (default 5).
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
   inputs into the final MKV.

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
                           flagging the other track.
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

The **reference audio is stream-copied** (`-c:a:0 copy`): it keeps its original
codec and bitrate bit-for-bit, with no generation loss. This matters because the
reference track is usually the one the library plays by default.

Only the **second track is encoded**, to AAC 192 kbit/s (`-c:a:1 aac -b:a:1
192k`). That is unavoidable — once an arbitrary audio filter (`adelay`/`atrim`)
is applied to fix the offset, the stream has to be rewritten. 192k is chosen
against the source rate: the recordings in this collection are MP3 128 kbit/s,
and roughly 1.5x the source rate is the point where an added generation stays
inaudible. Higher wastes space (the ceiling is the source), lower adds loss on
top of loss.

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