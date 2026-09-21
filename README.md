# audiosync v2

Small Linux tool for two Audials recordings of the **same movie release**,
one recorded with German audio and one with English audio.

## Requirements

    sudo apt install ffmpeg python3-numpy

## Usage

    ./audiosync.py film-de.mkv film-en.mkv -o film.mkv

The first file is the reference. Its video and first audio track are retained.
The first audio track from the second file is added as the English track.

## What V2 does

1. Checks the video stream parameters.
2. Decodes sample frames from both files and compares downscaled grayscale
   structural signatures at several positions. Recordings of the same release
   can start at slightly different times (different channel padding), so a
   constant video offset is first estimated and applied before comparing.
3. Extracts both first audio tracks as low-rate mono PCM solely for analysis.
4. Determines the relative audio offset at five positions throughout the movie.
5. Uses the median offset and rejects measurements that disagree by more than
   150 ms.
6. Positive offset: trims the leading part of the second audio with `atrim` and
   resets its PTS with `asetpts=PTS-STARTPTS` (the second audio starts later,
   so its leading silence is cut and it is advanced).
7. Negative offset: delays the second audio with `adelay` (the second audio
   starts earlier, so it is pushed back to align with the reference).
8. Muxes the reference video + both audio tracks into the final MKV.

## Important

The reference video is copied with `-c:v copy`, so there is **no video
re-encoding**.

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

    +1.250s  English starts 1.25 seconds later -> cut first 1.25 s (advance)
    -1.250s  English starts 1.25 seconds earlier -> delay English by 1.25 s

The tool deliberately aborts when the offset changes substantially during the
movie. That usually indicates different cuts, PAL/NTSC speed differences,
extra/missing scenes, or a bad correlation.
