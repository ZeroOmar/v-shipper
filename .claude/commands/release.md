Release a new version of v-shipper.

## Steps

### 1. Determine the next version

If the user specified a version explicitly in their message, use that.

Otherwise, read the current version from `app/__init__.py` (`__version__ = "x.y.z"`) and decide the bump by reviewing the diff (step 2 below). Apply semver rules:

- **major** (x+1.0.0) — breaking changes: removed/renamed API endpoints, config keys removed or incompatibly changed, data migration required
- **minor** (x.y+1.0) — new user-visible features added in a backwards-compatible way: new endpoints, new UI sections, new config options, new integrations
- **patch** (x.y.z+1) — bug fixes, internal refactors, style/copy changes, dependency bumps, documentation only

When in doubt between two levels, pick the higher one. State your reasoning in one sentence before proceeding.

### 2. Collect changes since the last release

Run both commands to understand what changed:
```bash
git log $(git describe --tags --abbrev=0)..HEAD --oneline
git diff $(git describe --tags --abbrev=0)..HEAD -- app/
```

Group the changes into categories: Fixed, Security, Changed, Added, Removed.
Write clear human-readable entries — not just commit hashes.

Then finalize the version bump decision from step 1 based on what you found.

### 3. Update version in source

Edit `app/__init__.py`: change `__version__ = "OLD"` → `__version__ = "NEW"`.

### 4. Write the release notes

There is no CHANGELOG.md — each release's notes live in its **annotated git tag message**, and CI publishes them to the GitHub Releases page (`.github/workflows/release.yml`). Write the notes to a temporary file (e.g. in the scratchpad dir) so they can be passed to `git tag -F` in step 6:

```
NEW_VERSION

### Fixed
- ...

### Changed
- ...
```

The first line is the tag subject (just the version). Then the grouped notes. Only include categories that have entries (Fixed, Security, Changed, Added, Removed). Keep the house style: bold lead phrase, em dash, then a clear explanation — the same prose quality the changelog used to carry, since this text becomes the GitHub Release body verbatim.

### 5. Update README.md and other docs

Scan for any references to the old version number and update them.
If new features, config options, or API endpoints were added, update the relevant sections.
Review CLAUDE.md for anything that needs updating (key files, architecture patterns, known issues list).

### 6. Stage, commit, tag, push

Use an **annotated** tag whose message is the notes file from step 4 — pushing the tag is what triggers CI to create the GitHub Release:

```bash
git add -A
git commit -m "NEW_VERSION - BRIEF_SUMMARY

LONGER_DESCRIPTION_IF_NEEDED"
git tag -a NEW_VERSION --cleanup=verbatim -F <notes-file>   # annotated: message becomes the GitHub Release body
git push
git push origin NEW_VERSION
```

The commit message subject should be `{version} - {one-line summary of the most significant change}`.

### 7. Confirm the release published

Pushing the tag triggers `.github/workflows/release.yml`, which reads the annotated tag message and publishes it to the GitHub Releases page. Confirm the run succeeded (the release should appear at `https://github.com/ZeroOmar/v-shipper/releases/tag/NEW_VERSION`).

## Important

- Do not skip the diff review — release notes must reflect actual code changes, not guesses.
- The tag **must be annotated** (`git tag -a`). A lightweight tag has no message, so CI would fall back to auto-generated commit-title notes instead of your curated prose.
- Always pass `--cleanup=verbatim` when tagging. Without it, git strips lines starting with `#` as comments — which silently deletes Markdown `###` section headers from the notes.
- Do not create a release if there are uncommitted changes unrelated to the release (ask the user first).
- Confirm the tag and push steps with the user before running them, since they are not reversible.
