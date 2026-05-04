#!/usr/bin/env python3
"""
YouTube playlist transcript extractor.

Backend: yt-dlp Python lib for enumeration + subtitle download.
Output per video: <idx>-<id>-<slug>.md (readable blocks) + .raw.json (preserved segments).
Per playlist: manifest.json (every video with status) + README.md (index).

Philosophy: preserve original aggressively. Don't sentence-merge ASR captions —
group into timestamped blocks by pause threshold; raw segments always available.
"""

import argparse
import hashlib
import html
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import webvtt
import yaml
from slugify import slugify
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError


# ---------------------------------------------------------------------------
# Status taxonomy

class Status:
    DONE = "done"
    SKIPPED_EXISTING = "skipped_existing"
    SKIPPED_NO_TRANSCRIPT = "skipped_no_transcript"
    FAILED_VIDEO_UNAVAILABLE = "failed_video_unavailable"
    FAILED_AGE_RESTRICTED = "failed_age_restricted"
    FAILED_REQUEST_BLOCKED = "failed_request_blocked"
    FAILED_RATE_LIMITED = "failed_rate_limited"
    FAILED_UNEXPECTED = "failed_unexpected"


STATUS_LABEL = {
    Status.DONE: "ok",
    Status.SKIPPED_EXISTING: "skip(exists)",
    Status.SKIPPED_NO_TRANSCRIPT: "skip(no transcript)",
    Status.FAILED_VIDEO_UNAVAILABLE: "fail(unavailable)",
    Status.FAILED_AGE_RESTRICTED: "fail(age)",
    Status.FAILED_REQUEST_BLOCKED: "fail(blocked)",
    Status.FAILED_RATE_LIMITED: "fail(rate)",
    Status.FAILED_UNEXPECTED: "fail(other)",
}


class TranscriptNotFound(Exception):
    pass


class TransientError(Exception):
    pass


# ---------------------------------------------------------------------------
# VTT parsing + cleanup

INLINE_TS_RE = re.compile(r"<\d{1,2}:\d{2}:\d{2}\.\d{3}>")
HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def clean_cue_text(text: str) -> str:
    text = INLINE_TS_RE.sub("", text)
    text = HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_vtt_segments(vtt_path: Path) -> list[dict]:
    """Parse VTT into list of {start_sec, end_sec, text}. Webvtt-py handles cue boundaries."""
    segments = []
    for cue in webvtt.read(str(vtt_path)):
        text = clean_cue_text(cue.text)
        if not text:
            continue
        segments.append(
            {
                "start_sec": cue.start_in_seconds,
                "end_sec": cue.end_in_seconds,
                "text": text,
            }
        )
    return segments


def is_rolling_caption(segments: list[dict]) -> bool:
    """YouTube auto VTT uses a pair pattern: a zero-duration cue carries each new
    line of text, paired with a duration cue that re-shows the new line plus the
    previous line still on screen. Zero-duration ratio is the cleanest detector."""
    if len(segments) < 6:
        return False
    zero_dur = sum(1 for s in segments if s["start_sec"] >= s["end_sec"])
    return (zero_dur / len(segments)) > 0.3


def dedup_rolling(segments: list[dict]) -> list[dict]:
    """For YouTube auto rolling captions: zero-duration cues carry each new
    line of fresh text without the on-screen overlap. Keep only those.
    Fallback: if no zero-dur cues, return as-is."""
    zero_dur = [s for s in segments if s["start_sec"] >= s["end_sec"]]
    if not zero_dur:
        return segments
    return zero_dur


def make_blocks(
    segments: list[dict],
    pause_threshold: float = 2.5,
    max_block_sec: float = 75.0,
    chapters: list[dict] | None = None,
    is_rolling_dedup: bool = False,
) -> list[dict]:
    """Group segments into blocks.
    - When chapters are provided, each segment is assigned to the chapter that
      contains its start time. Blocks split whenever consecutive segments belong
      to different chapters. Each chapter ends up as exactly one block.
    - When no chapters: split by pauses >= pause_threshold (skipped if
      is_rolling_dedup since cues are regular-cadence) and by max_block_sec cap.
    Blocks, NOT sentences — sentence boundaries are not inferred for ASR.

    chapters format: [{"start_time": float, "title": str}, ...]
    """
    if not segments:
        return []

    chap_list = []
    for ch in chapters or []:
        st = ch.get("start_time")
        if st is None:
            continue
        chap_list.append({"start": float(st), "title": ch.get("title") or ""})
    chap_list.sort(key=lambda c: c["start"])

    use_chapters = bool(chap_list)
    if use_chapters:
        pause_threshold = float("inf")
        max_block_sec = float("inf")
    elif is_rolling_dedup:
        pause_threshold = float("inf")

    def chapter_for(t: float) -> str:
        """Return the chapter title containing time t (last chapter whose start <= t)."""
        title = ""
        for c in chap_list:
            if c["start"] <= t:
                title = c["title"]
            else:
                break
        return title

    def new_block(seg: dict) -> dict:
        return {
            "start_sec": seg["start_sec"],
            "end_sec": seg["end_sec"],
            "lines": [seg["text"]],
            "chapter": chapter_for(seg["start_sec"]) if use_chapters else "",
        }

    blocks = []
    current = new_block(segments[0])
    for seg in segments[1:]:
        gap = seg["start_sec"] - current["end_sec"]
        block_dur = current["end_sec"] - current["start_sec"]
        chapter_changed = (
            use_chapters and chapter_for(seg["start_sec"]) != current["chapter"]
        )
        if chapter_changed or gap >= pause_threshold or block_dur >= max_block_sec:
            blocks.append(current)
            current = new_block(seg)
        else:
            current["end_sec"] = seg["end_sec"]
            current["lines"].append(seg["text"])
    blocks.append(current)
    return blocks


def fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# yt-dlp wrappers

def classify_yt_error(exc: BaseException) -> str:
    msg = str(exc).lower()
    if any(s in msg for s in ("private", "removed", "video unavailable", "deleted", "removed by")):
        return Status.FAILED_VIDEO_UNAVAILABLE
    if any(s in msg for s in ("age", "sign in to confirm", "login required")):
        return Status.FAILED_AGE_RESTRICTED
    if "429" in msg or "rate" in msg:
        return Status.FAILED_RATE_LIMITED
    if any(s in msg for s in ("blocked", "captcha", "bot")):
        return Status.FAILED_REQUEST_BLOCKED
    return Status.FAILED_UNEXPECTED


def _discover_tracks(video_id: str) -> dict:
    """Probe-only: get info + available subtitle tracks. No subtitle download."""
    opts = {
        "skip_download": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "quiet": True,
        "no_warnings": True,
    }
    url = f"https://www.youtube.com/watch?v={video_id}"
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise DownloadError(f"yt-dlp returned None for {video_id}")
    return info


def _download_one_track(
    video_id: str, lang: str, source: str, work_dir: Path
) -> Path:
    """Download a single subtitle track in a single language. Avoids translated-track 429."""
    work_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "skip_download": True,
        "writesubtitles": (source == "manual"),
        "writeautomaticsub": (source == "auto"),
        "subtitleslangs": [lang],
        "subtitlesformat": "vtt",
        "outtmpl": str(work_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
    }
    url = f"https://www.youtube.com/watch?v={video_id}"
    with YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)

    candidate = work_dir / f"{video_id}.{lang}.vtt"
    if candidate.exists():
        return candidate
    for f in work_dir.glob(f"{video_id}.*.vtt"):
        return f
    raise TranscriptNotFound(f"Sub file not written for {video_id}.{lang}")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type(TransientError),
    reraise=True,
)
def fetch_video_subs(video_id: str, langs: list[str], work_dir: Path) -> dict:
    """
    Two-call flow: discover → download only the chosen language.
    Avoids yt-dlp attempting translated tracks for langs that aren't actually present
    (which would 429 us).

    Returns {info, sub_path, source, lang}.
    Raises TranscriptNotFound if no caption track in any preferred language.
    Raises TransientError for transient network errors (caller retries).
    """
    try:
        info = _discover_tracks(video_id)
    except DownloadError as e:
        msg = str(e).lower()
        if any(s in msg for s in ("timed out", "timeout", "connection", "temporarily")) or "429" in msg:
            raise TransientError(str(e)) from e
        raise

    manual_keys = list((info.get("subtitles") or {}).keys())
    auto_dict = info.get("automatic_captions") or {}

    # Distinguish native auto from auto-translated. yt-dlp lists translated tracks
    # in `automatic_captions` too, but the URLs carry `tlang=` query param.
    # Translated tracks frequently 429 and aren't original creator audio anyway.
    def _is_native(tracks: list) -> bool:
        for t in tracks or []:
            if "tlang=" not in (t.get("url") or ""):
                return True
        return False

    native_auto_keys = [k for k, v in auto_dict.items() if _is_native(v)]

    chosen_lang = None
    source = None
    for lang in langs:
        if lang in manual_keys:
            chosen_lang, source = lang, "manual"
            break
    if chosen_lang is None:
        for lang in langs:
            if lang in native_auto_keys:
                chosen_lang, source = lang, "auto"
                break

    if chosen_lang is None:
        raise TranscriptNotFound(
            f"No native subtitles for langs={langs} "
            f"(manual={manual_keys}, native_auto={native_auto_keys[:5]}...)"
        )

    try:
        sub_path = _download_one_track(video_id, chosen_lang, source, work_dir)
    except DownloadError as e:
        msg = str(e).lower()
        if "429" in msg or any(s in msg for s in ("timed out", "timeout", "connection", "temporarily")):
            raise TransientError(str(e)) from e
        raise

    return {"info": info, "sub_path": sub_path, "source": source, "lang": chosen_lang}


# ---------------------------------------------------------------------------
# Output writing

def write_outputs(
    fetch_result: dict,
    playlist_index: int,
    output_dir: Path,
) -> tuple[Path, Path, dict]:
    info = fetch_result["info"]
    sub_path: Path = fetch_result["sub_path"]
    source = fetch_result["source"]
    lang = fetch_result["lang"]

    raw_segments = parse_vtt_segments(sub_path)
    rolling = is_rolling_caption(raw_segments)
    segments = dedup_rolling(raw_segments) if rolling else raw_segments
    blocks = make_blocks(
        segments, chapters=info.get("chapters"), is_rolling_dedup=rolling
    )

    title = info.get("title") or info["id"]
    slug = slugify(title, max_length=60) or info["id"]
    base = f"{playlist_index:02d}-{info['id']}-{slug}"

    md_path = output_dir / f"{base}.md"
    raw_path = output_dir / f"{base}.raw.json"

    fm = {
        "video_id": info["id"],
        "title": title,
        "channel": info.get("channel") or info.get("uploader"),
        "uploader_id": info.get("uploader_id"),
        "duration_sec": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={info['id']}",
        "playlist_index": playlist_index,
        "source": source,
        "language_code": lang,
        "is_generated": (source == "auto"),
        "block_count": len(blocks),
        "segment_count": len(raw_segments),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "cleanup_version": "v0-blocks-only",
        "rolling_dedup_applied": rolling,
    }

    lines = []
    lines.append("---")
    lines.append(yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).rstrip())
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"**Channel**: {fm['channel']}  ")
    lines.append(f"**URL**: {fm['url']}  ")
    lines.append(f"**Source**: {source} ({lang})")
    lines.append("")

    chapters = info.get("chapters") or []
    if chapters:
        lines.append("## Chapters")
        for ch in chapters:
            t = fmt_ts(ch.get("start_time") or 0)
            lines.append(f"- {t} — {ch.get('title','')}")
        lines.append("")

    lines.append("## Transcript")
    lines.append("")
    has_chapters = bool(info.get("chapters"))
    if has_chapters:
        split_desc = "one block per YouTube chapter"
    elif rolling:
        split_desc = (
            "75s max-block (no chapters; rolling-dedup auto VTT — pause data "
            "is meaningless after dedup)"
        )
    else:
        split_desc = "no chapters; split by pauses >= 2.5s or 75s max-block"
    note = (
        f"_Source: **{source}** ({lang}). "
        f"{len(blocks)} blocks, {len(raw_segments)} raw segments. "
        f"Sentence boundaries NOT inferred — {split_desc}. "
        f"Raw segments preserved in `{base}.raw.json`._"
    )
    lines.append(note)
    lines.append("")

    for b in blocks:
        ts = fmt_ts(b["start_sec"])
        text = " ".join(b["lines"]).strip()
        chapter = b.get("chapter") or ""
        header = f"### [{ts}] {chapter}".rstrip() if chapter else f"### [{ts}]"
        lines.append(header)
        lines.append("")
        lines.append(text)
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")

    raw_path.write_text(
        json.dumps(
            {
                "video_id": info["id"],
                "source": source,
                "language_code": lang,
                "rolling_dedup_applied": rolling,
                "segments": raw_segments,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    try:
        sub_path.unlink()
    except Exception:
        pass

    return md_path, raw_path, fm


# ---------------------------------------------------------------------------
# Manifest + README

def load_manifest(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"videos": {}, "playlist": {}}


def save_manifest(path: Path, manifest: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_readme(output_dir: Path, manifest: dict) -> None:
    videos = manifest.get("videos", {})
    pl = manifest.get("playlist", {})

    counts = {}
    for v in videos.values():
        counts[v.get("status", "unknown")] = counts.get(v.get("status", "unknown"), 0) + 1

    total = sum(counts.values())
    extracted = counts.get(Status.DONE, 0) + counts.get(Status.SKIPPED_EXISTING, 0)

    lines = []
    lines.append(f"# {pl.get('title', 'Playlist')}")
    lines.append("")
    lines.append(f"**Playlist URL**: {pl.get('url','')}  ")
    lines.append(f"**Playlist ID**: `{pl.get('id','')}`  ")
    if pl.get("uploader"):
        lines.append(f"**Uploader**: {pl.get('uploader')}  ")
    lines.append(f"**Last updated**: {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total in playlist: **{pl.get('video_count', total)}**")
    lines.append(f"- Recorded in manifest: **{total}**")
    lines.append(f"- Available as markdown: **{extracted}**")
    for status, count in sorted(counts.items()):
        if count > 0:
            lines.append(f"- {STATUS_LABEL.get(status, status)}: {count}")
    lines.append("")
    lines.append("## Videos")
    lines.append("")
    lines.append("| # | Title | Status | File | URL |")
    lines.append("|---|-------|--------|------|-----|")
    for _, v in sorted(videos.items(), key=lambda kv: kv[1].get("playlist_index", 0)):
        idx = v.get("playlist_index", "?")
        title = (v.get("title") or "").replace("|", "\\|")
        status = STATUS_LABEL.get(v.get("status"), v.get("status", ""))
        md_file = v.get("md_file", "")
        md_link = f"[{md_file}]({md_file})" if md_file else "—"
        url = v.get("url", "")
        url_link = f"[link]({url})" if url else ""
        lines.append(f"| {idx} | {title} | {status} | {md_link} | {url_link} |")
    lines.append("")
    (output_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Playlist enumeration

def enumerate_playlist(url: str) -> dict:
    opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    with YoutubeDL(opts) as ydl:
        data = ydl.extract_info(url, download=False)
    if data is None or data.get("_type") != "playlist":
        raise ValueError("Not a playlist URL or playlist could not be enumerated")
    return data


def existing_md_for(video_id: str, output_dir: Path) -> Path | None:
    for f in output_dir.glob(f"*-{video_id}-*.md"):
        return f
    return None


# ---------------------------------------------------------------------------
# Main

def main() -> int:
    p = argparse.ArgumentParser(
        description="Extract YouTube playlist transcripts into markdown."
    )
    p.add_argument("url", help="Playlist URL (or video URL with list= param)")
    p.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output directory (default: ~/yt-transcript/output/<playlist_id>)",
    )
    p.add_argument(
        "--langs",
        default="en,ko",
        help="Subtitle language priority (comma-separated, default: en,ko)",
    )
    p.add_argument("--force", action="store_true", help="Overwrite existing .md files")
    p.add_argument(
        "--limit", type=int, default=None, help="Process only first N videos (testing)"
    )
    p.add_argument("--start", type=int, default=1, help="Start at this playlist index (1-based)")
    p.add_argument("--delay-min", type=float, default=0.5)
    p.add_argument("--delay-max", type=float, default=2.0)
    args = p.parse_args()

    langs = [s.strip() for s in args.langs.split(",") if s.strip()]
    if not langs:
        print("ERROR: --langs is empty", file=sys.stderr)
        return 2

    print(f"Enumerating: {args.url}", file=sys.stderr)
    try:
        pl = enumerate_playlist(args.url)
    except Exception as e:
        print(f"ERROR: enumeration failed: {e}", file=sys.stderr)
        return 1

    pl_id = pl.get("id") or hashlib.md5(args.url.encode()).hexdigest()[:10]
    pl_title = pl.get("title", "playlist")
    entries = pl.get("entries") or []
    print(f"Playlist '{pl_title}' has {len(entries)} videos", file=sys.stderr)

    output_dir = (
        Path(args.output).expanduser().resolve()
        if args.output
        else Path.home() / "yt-transcript" / "output" / pl_id
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = output_dir / ".work"
    work_dir.mkdir(exist_ok=True)

    manifest_path = output_dir / "manifest.json"
    manifest = load_manifest(manifest_path)
    manifest["playlist"] = {
        "id": pl_id,
        "title": pl_title,
        "url": args.url,
        "uploader": pl.get("uploader"),
        "video_count": len(entries),
    }
    save_manifest(manifest_path, manifest)

    processed = 0
    for entry in entries:
        idx = entry.get("playlist_index")
        if idx is None:
            idx = entries.index(entry) + 1
        if idx < args.start:
            continue
        if args.limit and processed >= args.limit:
            break
        processed += 1

        vid_id = entry["id"]
        title = entry.get("title") or vid_id
        url = entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}"
        prefix = f"[{idx}/{len(entries)}] {vid_id}"

        existing = existing_md_for(vid_id, output_dir)
        if existing and not args.force:
            print(f"{prefix} skip (exists: {existing.name})", file=sys.stderr)
            manifest["videos"][vid_id] = {
                "playlist_index": idx,
                "title": title,
                "url": url,
                "status": Status.SKIPPED_EXISTING,
                "md_file": existing.name,
            }
            save_manifest(manifest_path, manifest)
            continue

        try:
            result = fetch_video_subs(vid_id, langs, work_dir)
        except TranscriptNotFound:
            print(f"{prefix} no transcript", file=sys.stderr)
            manifest["videos"][vid_id] = {
                "playlist_index": idx,
                "title": title,
                "url": url,
                "status": Status.SKIPPED_NO_TRANSCRIPT,
            }
            save_manifest(manifest_path, manifest)
            continue
        except TransientError as e:
            msg = str(e).lower()
            status = Status.FAILED_RATE_LIMITED if "429" in msg else Status.FAILED_REQUEST_BLOCKED
            print(f"{prefix} {status} (retries exhausted): {e}", file=sys.stderr)
            manifest["videos"][vid_id] = {
                "playlist_index": idx,
                "title": title,
                "url": url,
                "status": status,
                "error": str(e)[:500],
            }
            save_manifest(manifest_path, manifest)
            time.sleep(10.0)  # cool down on rate limit
            continue
        except (DownloadError, ExtractorError) as e:
            status = classify_yt_error(e)
            print(f"{prefix} {status}: {e}", file=sys.stderr)
            manifest["videos"][vid_id] = {
                "playlist_index": idx,
                "title": title,
                "url": url,
                "status": status,
                "error": str(e)[:500],
            }
            save_manifest(manifest_path, manifest)
            continue
        except Exception as e:
            print(f"{prefix} unexpected: {e}", file=sys.stderr)
            manifest["videos"][vid_id] = {
                "playlist_index": idx,
                "title": title,
                "url": url,
                "status": Status.FAILED_UNEXPECTED,
                "error": str(e)[:500],
            }
            save_manifest(manifest_path, manifest)
            continue

        try:
            md_path, raw_path, fm = write_outputs(result, idx, output_dir)
        except Exception as e:
            print(f"{prefix} write_failed: {e}", file=sys.stderr)
            manifest["videos"][vid_id] = {
                "playlist_index": idx,
                "title": title,
                "url": url,
                "status": Status.FAILED_UNEXPECTED,
                "error": f"write_outputs: {e}",
            }
            save_manifest(manifest_path, manifest)
            continue

        info = result["info"]
        manifest["videos"][vid_id] = {
            "playlist_index": idx,
            "title": info.get("title", title),
            "url": info.get("webpage_url", url),
            "channel": info.get("channel") or info.get("uploader"),
            "duration_sec": info.get("duration"),
            "upload_date": info.get("upload_date"),
            "status": Status.DONE,
            "source": result["source"],
            "language_code": result["lang"],
            "rolling_dedup_applied": fm.get("rolling_dedup_applied", False),
            "block_count": fm.get("block_count"),
            "segment_count": fm.get("segment_count"),
            "md_file": md_path.name,
            "raw_file": raw_path.name,
        }
        save_manifest(manifest_path, manifest)
        print(
            f"{prefix} done -> {md_path.name} (source={result['source']}, lang={result['lang']}, blocks={fm.get('block_count')})",
            file=sys.stderr,
        )

        time.sleep(random.uniform(args.delay_min, args.delay_max))

    # cleanup work dir
    try:
        for f in work_dir.glob("*"):
            try:
                f.unlink()
            except Exception:
                pass
        work_dir.rmdir()
    except Exception:
        pass

    write_readme(output_dir, manifest)

    counts = {}
    for v in manifest["videos"].values():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    total = sum(counts.values())
    print("", file=sys.stderr)
    print("=== Complete ===", file=sys.stderr)
    print(f"Processed this run: {processed}", file=sys.stderr)
    print(f"Total in manifest:  {total}", file=sys.stderr)
    for s, c in sorted(counts.items()):
        print(f"  {STATUS_LABEL.get(s, s):28s} {c}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"Output: {output_dir}", file=sys.stderr)
    print(f"Index:  {output_dir / 'README.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
