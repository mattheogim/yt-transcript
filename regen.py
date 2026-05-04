#!/usr/bin/env python3
"""
Regenerate .md files from existing .raw.json sidecars without calling yt-dlp.
Use this when block-splitting / dedup logic in extract.py changes and you want
to refresh outputs without hitting YouTube again.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

import extract


def regen_one(md_path: Path, raw_path: Path) -> tuple[int, int, bool]:
    raw_data = json.loads(raw_path.read_text(encoding="utf-8"))
    raw_segments = raw_data["segments"]
    source = raw_data["source"]
    lang = raw_data["language_code"]

    # re-apply current cleaning logic in case it changed (e.g. html.unescape added)
    for s in raw_segments:
        s["text"] = extract.clean_cue_text(s["text"])

    # parse existing frontmatter for the metadata we need
    md_text = md_path.read_text(encoding="utf-8")
    if not md_text.startswith("---"):
        raise ValueError(f"{md_path}: no frontmatter")
    end = md_text.find("\n---", 3)
    if end < 0:
        raise ValueError(f"{md_path}: frontmatter not closed")
    fm = yaml.safe_load(md_text[3:end])
    body_after = md_text[end + 4:]

    # extract chapter list from existing body if present
    chapters = []
    in_chapters = False
    for line in body_after.splitlines():
        if line.strip() == "## Chapters":
            in_chapters = True
            continue
        if in_chapters:
            if line.startswith("## "):
                break
            if line.startswith("- "):
                chapters.append(line)

    # rebuild segments + blocks with current logic
    rolling = extract.is_rolling_caption(raw_segments)
    segments = extract.dedup_rolling(raw_segments) if rolling else raw_segments

    # we don't have raw chapter starts for make_blocks; reconstruct from chapter lines
    chapter_starts = []
    import re
    for ch in chapters:
        m = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)\s+—", ch)
        if m:
            parts = m.group(1).split(":")
            if len(parts) == 2:
                chapter_starts.append(int(parts[0]) * 60 + int(parts[1]))
            elif len(parts) == 3:
                chapter_starts.append(int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))

    blocks = extract.make_blocks(
        segments, chapter_starts=chapter_starts, is_rolling_dedup=rolling
    )

    # rebuild frontmatter — keep most fields, update the changed ones
    fm["block_count"] = len(blocks)
    fm["segment_count"] = len(raw_segments)
    fm["rolling_dedup_applied"] = rolling
    fm["cleanup_version"] = "v0-blocks-only"
    fm["regenerated_at"] = datetime.now(timezone.utc).isoformat()

    base = md_path.stem  # e.g. "01-NSVmOC_5zrE-game-theory-101-1-introduction"

    lines = []
    lines.append("---")
    lines.append(yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).rstrip())
    lines.append("---")
    lines.append("")
    lines.append(f"# {fm.get('title')}")
    lines.append("")
    lines.append(f"**Channel**: {fm.get('channel')}  ")
    lines.append(f"**URL**: {fm.get('url')}  ")
    lines.append(f"**Source**: {source} ({lang})")
    lines.append("")

    if chapters:
        lines.append("## Chapters")
        lines.extend(chapters)
        lines.append("")

    lines.append("## Transcript")
    lines.append("")
    split_desc = (
        "split by chapter starts or 75s max-block (rolling-dedup auto VTT — "
        "pause data is meaningless after dedup)"
        if rolling
        else "split by chapter starts, pauses >= 2.5s, or 75s max-block"
    )
    lines.append(
        f"_Source: **{source}** ({lang}). "
        f"{len(blocks)} blocks, {len(raw_segments)} raw segments. "
        f"Sentence boundaries NOT inferred — {split_desc}. "
        f"Raw segments preserved in `{base}.raw.json`._"
    )
    lines.append("")

    for b in blocks:
        ts = extract.fmt_ts(b["start_sec"])
        text = " ".join(b["lines"]).strip()
        lines.append(f"### [{ts}]")
        lines.append("")
        lines.append(text)
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return len(blocks), len(raw_segments), rolling


def main() -> int:
    p = argparse.ArgumentParser(
        description="Regenerate .md files from .raw.json sidecars (no yt-dlp calls)."
    )
    p.add_argument("output_dir", help="Directory containing .md and .raw.json files")
    p.add_argument(
        "--source",
        default=None,
        help="Only regen videos with this source (manual|auto). Default: all.",
    )
    args = p.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    if not output_dir.is_dir():
        print(f"Not a directory: {output_dir}", file=sys.stderr)
        return 2

    raw_files = sorted(output_dir.glob("*.raw.json"))
    if not raw_files:
        print(f"No .raw.json found in {output_dir}", file=sys.stderr)
        return 1

    regenerated = 0
    skipped = 0
    failed = 0
    for raw_path in raw_files:
        base = raw_path.name.removesuffix(".raw.json")
        md_path = output_dir / f"{base}.md"
        if not md_path.exists():
            print(f"  no md companion: {raw_path.name}", file=sys.stderr)
            skipped += 1
            continue
        try:
            data = json.loads(raw_path.read_text(encoding="utf-8"))
            if args.source and data.get("source") != args.source:
                skipped += 1
                continue
        except Exception as e:
            print(f"  err read {raw_path.name}: {e}", file=sys.stderr)
            failed += 1
            continue

        try:
            blocks, segs, rolling = regen_one(md_path, raw_path)
            print(
                f"  {md_path.name}: blocks={blocks} segs={segs} rolling_dedup={rolling}",
                file=sys.stderr,
            )
            regenerated += 1
        except Exception as e:
            print(f"  err regen {md_path.name}: {e}", file=sys.stderr)
            failed += 1

    print(f"\nRegenerated: {regenerated}", file=sys.stderr)
    print(f"Skipped:     {skipped}", file=sys.stderr)
    print(f"Failed:      {failed}", file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
