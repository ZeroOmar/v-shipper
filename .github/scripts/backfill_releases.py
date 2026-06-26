#!/usr/bin/env python3
"""One-time backfill: turn each version section of CHANGELOG.md into a GitHub Release.

Parses CHANGELOG.md, and for every `## X.Y.Z` section that has a matching git tag
(`X.Y.Z` or `vX.Y.Z`), creates a GitHub Release whose body is that section. Idempotent:
versions that already have a release are skipped, so the workflow can be re-run safely.

Run inside CI (GitHub Actions) where `gh` and `GITHUB_TOKEN`/`GH_TOKEN` are available and
the repo is checked out with full tags (`fetch-depth: 0`). After this succeeds, CHANGELOG.md
is no longer the source of truth — new releases come from annotated tag messages.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

CHANGELOG = Path("CHANGELOG.md")
VERSION_HEADER = re.compile(r"^## (\d+\.\d+\.\d+)\s*$")


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def existing_tags() -> set[str]:
    out = sh("git", "tag").stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def tag_for_version(version: str, tags: set[str]) -> str | None:
    """Map a changelog version to the actual tag name (bare or v-prefixed)."""
    if version in tags:
        return version
    if f"v{version}" in tags:
        return f"v{version}"
    return None


def release_exists(tag: str) -> bool:
    return sh("gh", "release", "view", tag).returncode == 0


def parse_sections(text: str) -> list[tuple[str, str]]:
    """Return (version, body) pairs. A section runs from its `## X.Y.Z` header to the
    next *version* header (not any `##`), so trailing prose like 'Migration Guide'
    stays attached to the oldest release it documents."""
    lines = text.splitlines()
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = VERSION_HEADER.match(line)
        if m:
            starts.append((i, m.group(1)))

    sections = []
    for idx, (line_no, version) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        body = "\n".join(lines[line_no + 1:end]).strip()
        sections.append((version, body))
    return sections


def main() -> int:
    if not CHANGELOG.exists():
        print("CHANGELOG.md not found — nothing to backfill.")
        return 0

    tags = existing_tags()
    sections = parse_sections(CHANGELOG.read_text())
    print(f"Found {len(sections)} version section(s) in CHANGELOG.md, {len(tags)} git tag(s).")

    created, skipped_exists, skipped_no_tag = [], [], []
    for version, body in sections:
        tag = tag_for_version(version, tags)
        if not tag:
            skipped_no_tag.append(version)
            print(f"⏭  {version}: no matching tag — skipped")
            continue
        if release_exists(tag):
            skipped_exists.append(tag)
            print(f"⏭  {tag}: release already exists — skipped")
            continue

        notes = body or f"Release {version}."
        result = subprocess.run(
            ["gh", "release", "create", tag, "--title", tag, "--verify-tag", "--notes-file", "-"],
            input=notes, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"✗  {tag}: failed to create release\n{result.stderr.strip()}", file=sys.stderr)
            return 1
        created.append(tag)
        print(f"✓  {tag}: release created")

    print("\n── Summary ──────────────────────")
    print(f"{len(created)} created · {len(skipped_exists)} already existed · "
          f"{len(skipped_no_tag)} section(s) had no tag")
    if skipped_no_tag:
        print(f"   sections without a tag: {', '.join(skipped_no_tag)}")
    # Tags that exist but have no changelog section are left without a release intentionally.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
