# yt-transcript

Extract YouTube playlist transcripts into markdown files for later reading and cleanup.

Backend: `yt-dlp`. Output is one `.md` per video (readable, grouped into blocks) plus
a `.raw.json` sidecar that preserves the original timestamped segments verbatim.

## Design principles

1. **Preserve original aggressively.** v0 does not edit a single word. Block grouping is
   the only transformation applied to the markdown body. Raw segments stay in
   `.raw.json` for any future cleanup pass to diff against.
2. **Native captions only.** Manual creator-uploaded subtitles take priority. If a
   video only has auto-translated tracks (URL contains `tlang=`), it is skipped — those
   are double-machine-processed and not the creator's voice.
3. **Account for every video.** The manifest records every playlist entry with one of
   8 statuses (`done`, `skipped_no_transcript`, `skipped_existing`, `failed_*`).
   `README.md` is generated from the manifest with counts and a per-video table.
4. **Resume-friendly.** Re-running skips videos with existing `.md` unless `--force`.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python extract.py "https://www.youtube.com/playlist?list=YOUR_LIST_ID" \
  --langs en
```

Output goes to `~/yt-transcript/output/<playlist_id>/` by default. Use `-o` to override.

### Options

- `--langs en,ko` — language priority (manual takes precedence over auto)
- `--force` — overwrite existing `.md`
- `--limit N` — process only first N videos (for testing)
- `--start N` — start at playlist index N (1-based)
- `--delay-min` / `--delay-max` — jitter between videos (default 0.5–2.0s)

## Regenerate `.md` from `.raw.json`

If block-splitting or cleanup logic changes, refresh the markdown without
calling YouTube again:

```bash
.venv/bin/python regen.py output/<playlist_id>
```

## How block splitting works

Sentence boundaries are not inferred for ASR text. Blocks are split on:

- **Chapter starts** when YouTube chapters exist (typically the most useful boundary)
- **Pauses ≥ 2.5s** between cues (only for non-rolling sources — pause data is
  meaningless after rolling-dedup)
- **75s max-block** as a hard cap

YouTube auto-VTT uses a "rolling" pattern: each new line is delivered as a
zero-duration cue, then re-emitted in the next cue alongside the previous line
still on screen. The dedup keeps only the zero-duration cues, which carry each
fresh line exactly once.

## File layout

```
extract.py         # main extractor (~660 lines)
regen.py           # rebuild .md from .raw.json without yt-dlp
requirements.txt
output/<id>/
  README.md        # generated: index + counts + per-video table
  manifest.json    # generated: status for every playlist video
  NN-<id>-<slug>.md       # readable, blocks
  NN-<id>-<slug>.raw.json # original timestamped segments (verbatim)
```

`output/` is gitignored — transcripts are creator content, kept local.
