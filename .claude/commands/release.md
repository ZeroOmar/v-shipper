Release a new version of v-shipper.

## Steps

### 1. Determine the next version

Read the current version from `app/models.py` (field `version: str = "0.0.x"`).
Increment the patch number by 1 (e.g. 0.0.7 → 0.0.8).
If the user specified a version in their message, use that instead.

### 2. Collect changes since the last release

Run:
```bash
git log $(git describe --tags --abbrev=0)..HEAD --oneline
```

Read the diff of changed files to understand what actually changed:
```bash
git diff $(git describe --tags --abbrev=0)..HEAD -- app/
```

Group the changes into categories: Fixed, Security, Changed, Added, Removed.
Write clear human-readable entries — not just commit hashes.

### 3. Update version in source

Edit `app/models.py`: change `version: str = "OLD"` → `version: str = "NEW"`.

### 4. Update CHANGELOG.md

Prepend a new section after the `# Changelog` header line:

```
## NEW_VERSION

### Fixed
- ...

### Changed
- ...
```

Only include categories that have entries. Keep the style consistent with existing entries (bold lead phrase, em dash, explanation).

### 5. Update README.md and other docs

Scan for any references to the old version number and update them.
If new features, config options, or API endpoints were added, update the relevant sections.
Review CLAUDE.md for anything that needs updating (key files, architecture patterns, known issues list).

### 6. Stage, commit, tag, push

```bash
git add -A
git commit -m "NEW_VERSION - BRIEF_SUMMARY

LONGER_DESCRIPTION_IF_NEEDED"
git tag NEW_VERSION
git push
git push origin NEW_VERSION
```

The commit message subject should be `{version} - {one-line summary of the most significant change}`.

## Important

- Do not skip the diff review — changelog entries must reflect actual code changes, not guesses.
- Do not create a release if there are uncommitted changes unrelated to the release (ask the user first).
- Confirm the tag and push steps with the user before running them, since they are not reversible.
