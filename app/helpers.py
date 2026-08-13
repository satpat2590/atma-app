"""Shared helpers — lesson-title parsing, score colour, lesson index."""

from pathlib import Path


def _parse_lesson_title(md_path: Path) -> str:
    """Extract human-readable title from markdown H1, falling back to filename."""
    try:
        first_line = md_path.read_text().split("\n", 1)[0].strip()
        if first_line.startswith("# "):
            title = first_line[2:].strip()
            # Strip date suffix like " — 2026-08-07"
            if " — " in title:
                title = title.rsplit(" — ", 1)[0]
            return title
    except Exception:
        pass
    return md_path.stem.replace("-", " ").title()


def _score_color(score: float) -> str:
    if score >= 1.0:
        return "green"
    if score >= 0.4:
        return "amber"
    return "red"


def build_lesson_index(curriculum_dir: Path) -> dict:
    """Return {lower_title: relative_path} for every .md in curriculum tree."""
    index = {}
    if curriculum_dir.exists():
        for domain_dir in curriculum_dir.iterdir():
            if not domain_dir.is_dir():
                continue
            for md_file in domain_dir.glob("*.md"):
                if md_file.name.startswith("gyani-"):
                    continue
                index[_parse_lesson_title(md_file).lower()] = str(
                    md_file.relative_to(Path.home()))
    return index