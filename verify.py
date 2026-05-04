#!/usr/bin/env python3
"""
Coverage verification for extracted transcripts.

Flags potential content cut-off and structural anomalies:
- Last raw segment ends well before duration_sec (transcript likely truncated)
- Block count mismatches chapter count (for chaptered videos)
- Segments missing from the rendered .md (no segment dropped)
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml


def parse_md(md_path: Path) -> dict:
    text = md_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {"frontmatter": {}, "chapter_lines": [], "block_headers": [], "body": text}
    end = text.find("\n---", 3)
    fm = yaml.safe_load(text[3:end]) or {}
    body = text[end + 4:]
    chapter_lines = []
    block_headers = []
    in_chap = False
    for line in body.splitlines():
        if line.strip() == "## Chapters":
            in_chap = True
            continue
        if in_chap:
            if line.startswith("## "):
                in_chap = False
            elif line.startswith("- "):
                chapter_lines.append(line)
        if line.startswith("### "):
            block_headers.append(line)
    return {
        "frontmatter": fm,
        "chapter_lines": chapter_lines,
        "block_headers": block_headers,
        "body": body,
    }


def check_one(md_path: Path, raw_path: Path) -> list[dict]:
    issues = []
    md = parse_md(md_path)
    fm = md["frontmatter"]
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    segs = raw["segments"]

    duration = fm.get("duration_sec")
    last_end = max((s["end_sec"] for s in segs), default=0)

    # 1) coverage gap — last segment end vs duration
    if duration is not None and duration > 0:
        gap = duration - last_end
        # coverage gap: > 30 seconds is suspicious; absolute and relative bands
        if gap > 30 and (gap / duration) > 0.05:
            issues.append({
                "kind": "coverage_gap",
                "duration_sec": duration,
                "last_segment_end_sec": last_end,
                "gap_sec": round(gap, 1),
                "gap_pct": round(100 * gap / duration, 1),
            })

    # 2) chapter / block alignment
    chapter_lines = md["chapter_lines"]
    block_headers = md["block_headers"]
    if chapter_lines:
        n_chap = len(chapter_lines)
        n_blk = len(block_headers)
        if n_chap != n_blk:
            issues.append({
                "kind": "chapter_block_mismatch",
                "chapters": n_chap,
                "blocks": n_blk,
            })

    # 3) word coverage — sum of block text words vs sum of segment words
    block_word_count = 0
    in_transcript = False
    for line in md["body"].splitlines():
        if line.strip() == "## Transcript":
            in_transcript = True
            continue
        if in_transcript:
            s = line.strip()
            if s.startswith("##") or s.startswith("###") or s.startswith("_Source"):
                continue
            if not s:
                continue
            block_word_count += len(s.split())

    seg_word_count = sum(len(s["text"].split()) for s in segs)
    rolling = raw.get("rolling_dedup_applied")

    if not rolling:
        # block words should ~= seg words (no dedup, no drops)
        if seg_word_count > 0:
            ratio = block_word_count / seg_word_count
            if ratio < 0.95 or ratio > 1.05:
                issues.append({
                    "kind": "word_count_drift",
                    "block_words": block_word_count,
                    "seg_words": seg_word_count,
                    "ratio": round(ratio, 3),
                    "rolling": False,
                })
    else:
        # rolling dedup keeps zero-duration cues only — expect ~half the segments
        zero_dur_words = sum(
            len(s["text"].split())
            for s in segs
            if s["start_sec"] >= s["end_sec"]
        )
        if zero_dur_words > 0:
            ratio = block_word_count / zero_dur_words
            if ratio < 0.95 or ratio > 1.05:
                issues.append({
                    "kind": "word_count_drift",
                    "block_words": block_word_count,
                    "zero_dur_words": zero_dur_words,
                    "seg_words_total": seg_word_count,
                    "ratio": round(ratio, 3),
                    "rolling": True,
                })

    return issues


def main() -> int:
    p = argparse.ArgumentParser(
        description="Verify coverage and structure of extracted transcripts."
    )
    p.add_argument("output_dir", help="Directory with .md and .raw.json files")
    p.add_argument("--quiet", action="store_true", help="Print only issues")
    args = p.parse_args()

    out = Path(args.output_dir).expanduser().resolve()
    raws = sorted(out.glob("*.raw.json"))

    total = 0
    flagged = 0
    issues_by_kind = {}

    for raw_path in raws:
        base = raw_path.name.removesuffix(".raw.json")
        md_path = out / f"{base}.md"
        if not md_path.exists():
            continue
        total += 1
        try:
            issues = check_one(md_path, raw_path)
        except Exception as e:
            print(f"  err checking {md_path.name}: {e}", file=sys.stderr)
            continue
        if issues:
            flagged += 1
            print(f"\n[{md_path.name}]")
            for issue in issues:
                kind = issue.pop("kind")
                issues_by_kind[kind] = issues_by_kind.get(kind, 0) + 1
                detail = ", ".join(f"{k}={v}" for k, v in issue.items())
                print(f"  {kind}: {detail}")
        elif not args.quiet:
            pass  # silent if no issues

    print()
    print(f"=== Coverage Verification Summary ===")
    print(f"Checked: {total}")
    print(f"Clean:   {total - flagged}")
    print(f"Flagged: {flagged}")
    if issues_by_kind:
        print()
        print("Issues by kind:")
        for kind, count in sorted(issues_by_kind.items()):
            print(f"  {kind}: {count}")
    return 0 if flagged == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
