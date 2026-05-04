#!/usr/bin/env python3
"""
Build a claude-tutor course from extracted YouTube transcripts.

Reads .md files from a yt-transcript output directory, groups videos into
thematic chapters, and writes the TutorAgent-compatible layout:

  {course}/{professor}/
    chapters/chapter_N/notes.md    # concatenated transcripts as sections
    meta_index.json                # course + chapter + section index
    knowledge_graph.json           # empty stub (TutorAgent populates)
    personal/                      # empty (student data)
    analytics/                     # empty (event log)

Word-for-word preservation per TutorAgent P1: transcripts are concatenated
verbatim under section headers. Frontmatter is dropped (it's metadata, not
content). Section header carries video number + YouTube URL.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


# Topical chapter groupings for William Spaniel's "Game Theory 101 Full Course".
# Indices are playlist positions (1-based, 86 videos total).
CHAPTERS = [
    {
        "num": 1,
        "title": "Simultaneous Move Games",
        "topics": [
            "prisoner's dilemma",
            "strict and weak dominance",
            "iterated elimination",
            "Nash equilibrium",
            "best responses",
            "mixed strategy basics",
            "matching pennies",
            "calculating payoffs",
            "infinitely many equilibria",
            "the odd rule",
        ],
        "indices": list(range(1, 16)),
    },
    {
        "num": 2,
        "title": "Extensive Form Games",
        "topics": [
            "subgame perfect equilibrium",
            "backward induction",
            "games with stages",
            "punishment strategies",
            "tying hands and burning bridges",
            "commitment problems",
            "centipede game",
            "problems with backward induction",
            "forward induction",
        ],
        "indices": list(range(16, 27)),
    },
    {
        "num": 3,
        "title": "Mixed Strategies and Advanced Strategic Form",
        "topics": [
            "probability distributions",
            "generalized battle of the sexes",
            "knife-edge equilibria",
            "soccer penalty kicks",
            "establishing causation",
            "comparative statics",
            "the support of mixed strategies",
            "weak dominance trick",
            "rock paper scissors",
            "symmetric zero-sum games",
            "mixing among three strategies",
            "duels",
            "Hotelling's game and the median voter",
            "second price auctions",
        ],
        "indices": list(range(27, 42)),
    },
    {
        "num": 4,
        "title": "Expected Utility Theory",
        "topics": [
            "expected utility",
            "completeness",
            "transitivity",
            "rationality",
            "Condorcet's paradox",
            "social preferences",
            "lotteries",
            "independence over lotteries",
            "Allais paradox",
            "continuity",
            "expected utility transformations",
            "Pareto efficiency",
            "risk averse, neutral, acceptant",
        ],
        "indices": list(range(42, 54)),
    },
    {
        "num": 5,
        "title": "Repeated Games",
        "topics": [
            "finite repeated prisoner's dilemma",
            "discount factors",
            "geometric series and infinite payoffs",
            "one-shot deviation principle",
            "grim trigger",
            "tit-for-tat",
            "tit-for-tat subgame perfection",
            "folk theorem",
            "prediction problem",
        ],
        "indices": list(range(54, 63)),
    },
    {
        "num": 6,
        "title": "Bayesian Games and Incomplete Information",
        "topics": [
            "incomplete information",
            "Bayesian Nash equilibrium",
            "ex ante and interim dominance",
            "antes in poker",
            "is more information always better",
            "cutpoint strategies",
            "continuous type space",
            "purification theorem",
            "Bayes' rule",
            "winner's curse",
        ],
        "indices": list(range(63, 74)),
    },
    {
        "num": 7,
        "title": "Signaling, Screening, and Perfect Bayesian Equilibrium",
        "topics": [
            "perfect Bayesian equilibrium",
            "screening games",
            "adverse selection",
            "signaling games",
            "separating equilibrium",
            "pooling equilibrium",
            "off-the-path beliefs",
            "beer-quiche game",
            "semi-separating equilibrium",
            "single raise poker",
            "chain store paradox",
        ],
        "indices": list(range(74, 87)),
    },
]


def parse_md_file(path: Path) -> dict:
    """Parse a yt-transcript .md and return frontmatter + body."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {"frontmatter": {}, "body": text}
    end = text.find("\n---", 3)
    fm = yaml.safe_load(text[3:end]) or {}
    body = text[end + 4:].lstrip("\n")
    return {"frontmatter": fm, "body": body}


def extract_chapters_block(body: str) -> tuple[str, str]:
    """Pull `## Chapters` block out of the body. Return (chapters_md, remainder)."""
    lines = body.splitlines()
    chap_lines = []
    rest = []
    in_chap = False
    chap_done = False
    for line in lines:
        if not chap_done and line.strip() == "## Chapters":
            in_chap = True
            chap_lines.append(line)
            continue
        if in_chap:
            if line.startswith("## "):
                in_chap = False
                chap_done = True
                rest.append(line)
            else:
                chap_lines.append(line)
        else:
            rest.append(line)
    return "\n".join(chap_lines).strip(), "\n".join(rest).rstrip()


def extract_transcript_block(body: str) -> str:
    """Return everything from `## Transcript` onward, with the source-note
    line stripped (we add our own context above)."""
    idx = body.find("## Transcript")
    if idx < 0:
        return body
    text = body[idx:]
    # drop the italic source-note paragraph that follows the heading
    lines = text.splitlines()
    out = [lines[0]]  # keep "## Transcript"
    skipped_note = False
    for line in lines[1:]:
        if not skipped_note and line.strip().startswith("_Source"):
            skipped_note = True
            continue
        out.append(line)
    return "\n".join(out).rstrip()


def build_section(
    section_num: str,
    md_data: dict,
) -> tuple[str, dict]:
    """Build markdown for a single section. Returns (section_md, section_meta)."""
    fm = md_data["frontmatter"]
    body = md_data["body"]

    title = fm.get("title", "Untitled")
    url = fm.get("url", "")
    source = fm.get("source", "?")
    lang = fm.get("language_code", "?")
    duration = fm.get("duration_sec", 0)
    upload = fm.get("upload_date", "")
    is_gen = fm.get("is_generated", False)

    chapters_block, rest = extract_chapters_block(body)
    transcript_block = extract_transcript_block(rest)

    parts = []
    parts.append(f"## Section {section_num}: {title}")
    parts.append("")
    parts.append(f"**Video URL**: {url}  ")
    parts.append(f"**Duration**: {fmt_duration(duration)} · **Uploaded**: {fmt_date(upload)}  ")
    src_note = f"caption-source: **{source}** ({lang})"
    if is_gen:
        src_note += " — auto-generated, may have ASR artifacts (lowercase, no punctuation, occasional misrecognized words)"
    parts.append(f"**Transcript**: {src_note}")
    parts.append("")
    if chapters_block:
        # rewrite "## Chapters" -> "### Video chapters" so it fits as a sub-section
        chap_lines = chapters_block.splitlines()
        chap_lines[0] = "### Video chapters"
        parts.extend(chap_lines)
        parts.append("")

    if transcript_block:
        # rewrite "## Transcript" -> "### Transcript" (sub-section under our Section)
        # and "### [HH:MM] Title" -> "#### [HH:MM] Title"
        rewritten = []
        for line in transcript_block.splitlines():
            if line.startswith("## Transcript"):
                rewritten.append("### Transcript")
            elif line.startswith("### ["):
                rewritten.append("#" + line)  # "### [..." -> "#### [..."
            else:
                rewritten.append(line)
        parts.append("\n".join(rewritten))
    parts.append("")

    meta = {
        "section": section_num,
        "title": title,
        "video_id": fm.get("video_id"),
        "playlist_index": fm.get("playlist_index"),
        "url": url,
        "duration_sec": duration,
        "upload_date": upload,
        "caption_source": source,
        "language_code": lang,
        "is_generated": is_gen,
        "block_count": fm.get("block_count"),
        "segment_count": fm.get("segment_count"),
    }
    return "\n".join(parts), meta


def fmt_duration(sec) -> str:
    if not sec:
        return "?"
    s = int(sec)
    return f"{s // 60}:{s % 60:02d}"


def fmt_date(yyyymmdd: str) -> str:
    if not yyyymmdd or len(yyyymmdd) != 8:
        return yyyymmdd or "?"
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def collect_md_files(transcript_dir: Path) -> dict[int, dict]:
    """Index .md files by playlist_index."""
    by_idx = {}
    for md in sorted(transcript_dir.glob("*.md")):
        if md.name == "README.md":
            continue
        try:
            data = parse_md_file(md)
            idx = data["frontmatter"].get("playlist_index")
            if idx is None:
                continue
            data["path"] = md
            by_idx[int(idx)] = data
        except Exception as e:
            print(f"  skip {md.name}: {e}", file=sys.stderr)
    return by_idx


def build_course(
    transcript_dir: Path,
    course_root: Path,
    course_name: str,
    professor: str,
    course_title: str,
    language: str,
    note_source: str,
) -> int:
    """Build the course tree under course_root. Returns number of sections written."""
    course_dir = course_root / course_name / professor
    chapters_dir = course_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    (course_dir / "personal").mkdir(exist_ok=True)
    (course_dir / "analytics").mkdir(exist_ok=True)

    by_idx = collect_md_files(transcript_dir)
    if not by_idx:
        print(f"No .md files with playlist_index found in {transcript_dir}", file=sys.stderr)
        return 0

    meta = {
        "course": course_name,
        "professor": professor,
        "course_title": course_title,
        "language": language,
        "base_path": str(course_dir),
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "note_source": note_source,
        "chapters": {},
    }

    sections_total = 0
    for chap in CHAPTERS:
        ch_num = chap["num"]
        ch_dir = chapters_dir / f"chapter_{ch_num}"
        ch_dir.mkdir(exist_ok=True)

        body_parts = [f"# Chapter {ch_num}: {chap['title']}", ""]
        section_ids = []
        section_metas = []

        seq = 1
        for plist_idx in chap["indices"]:
            if plist_idx not in by_idx:
                continue
            section_num = f"{ch_num}.{seq}"
            section_md, sec_meta = build_section(section_num, by_idx[plist_idx])
            body_parts.append(section_md)
            section_ids.append(section_num)
            section_metas.append(sec_meta)
            seq += 1

        if not section_ids:
            continue

        notes_md = "\n".join(body_parts).rstrip() + "\n"
        (ch_dir / "notes.md").write_text(notes_md, encoding="utf-8")

        meta["chapters"][f"chapter_{ch_num}"] = {
            "title": chap["title"],
            "notes_path": f"chapters/chapter_{ch_num}/notes.md",
            "section_plan_path": f"chapters/chapter_{ch_num}/section_plan.md",
            "sections": section_ids,
            "section_metadata": section_metas,
            "last_studied": None,
            "topics": chap["topics"],
            "source_files": [
                f"yt-transcript/output/{transcript_dir.name}/{by_idx[i]['path'].name}"
                for i in chap["indices"]
                if i in by_idx
            ],
            "status": "ready",
        }
        sections_total += len(section_ids)

    (course_dir / "meta_index.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    kg_path = course_dir / "knowledge_graph.json"
    if not kg_path.exists():
        kg_path.write_text(
            json.dumps({"nodes": {}, "edges": []}, indent=2),
            encoding="utf-8",
        )

    return sections_total


def main() -> int:
    p = argparse.ArgumentParser(
        description="Build a claude-tutor course from yt-transcript output."
    )
    p.add_argument("transcript_dir", help="yt-transcript output dir (the playlist directory)")
    p.add_argument(
        "--course-root",
        default=str(Path.home() / "claude-tutor"),
        help="claude-tutor base directory",
    )
    p.add_argument("--course", required=True, help="Course slug (e.g. gametheory101)")
    p.add_argument("--professor", required=True, help="Professor surname (e.g. Spaniel)")
    p.add_argument("--course-title", required=True, help="Full course title")
    p.add_argument("--language", default="English", help="Course language")
    p.add_argument("--note-source", default="", help="Source description for note_source field")
    args = p.parse_args()

    transcript_dir = Path(args.transcript_dir).expanduser().resolve()
    course_root = Path(args.course_root).expanduser().resolve()

    if not transcript_dir.is_dir():
        print(f"Transcript dir not found: {transcript_dir}", file=sys.stderr)
        return 2

    n = build_course(
        transcript_dir=transcript_dir,
        course_root=course_root,
        course_name=args.course,
        professor=args.professor,
        course_title=args.course_title,
        language=args.language,
        note_source=args.note_source,
    )

    course_path = course_root / args.course / args.professor
    print()
    print(f"=== Course built ===")
    print(f"Path:     {course_path}")
    print(f"Sections: {n}")
    print(f"Chapters: {sum(1 for ch in CHAPTERS if any((i in collect_md_files(transcript_dir)) for i in ch['indices']))}")
    print()
    print(f"Try in claude-tutor: cd {args.course_root} && claude")
    return 0


if __name__ == "__main__":
    sys.exit(main())
