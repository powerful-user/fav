---
title: "fav: wings-x as a standalone package and skill"
kind: spec
target: fav
topic: fav-standalone
status: draft
written: 2026-11-04T22:04:49Z
by: claude
worktree: /home/jmo/linda/repos/fav/.worktrees/spec-fav-standalone
---

# fav: wings-x as a standalone package and skill

## Goal

Publish wings-x, a read-only, spend-capped X command line for agents, as
`fav`: an MIT-licensed Python package anyone can install from PyPI or
Homebrew, with its agent skill shipped beside it. `fav` replaces wings-x
on the Wings host; afterwards there is one codebase and one copy of the
skill.

Done means:

1. `pipx install fav`, `uv tool install fav` and
   `brew install service-dept/tap/fav` each put a working `fav` 0.1.0 on
   PATH on Linux and macOS.
2. A new user can follow the README from nothing to a working
   `fav search` with their own X developer app.
3. On the Wings host, `fav` runs on the existing wings-x token, config
   and bookmark index without a new login or sync, `wings-x` is gone, and
   the installed `x-research` skill is the one from this repository.

The name comes from the pre-2015 X "fav" (the star, before likes).

## Current state

Checked 2026-11-04.

- **This repository:** `service-dept/fav`, public, at `5076011`
  (`chore: initialize fav`): `README.md` (`# fav`) and an empty `docs/`.
- **wings-x:** private `service-dept/x-runtime` at `e6465c6` on `main`,
  tags `v0.1.0` and `v0.2.0`. Python standard library only, `unittest`.
  Package `wings_x/` (16 modules), a development launcher `bin/wings-x`,
  an `./install <version>` script that exports a tag into
  `$XDG_DATA_HOME/wings-x/releases/<version>/` and links
  `~/.local/bin/wings-x`, and `tests/` with fixtures and
  `tests/live_smoke.sh`. Its approved spec and plan
  (`docs/2026-10-02-x-runtime-spec-wings-x.md`,
  `docs/2026-10-02-x-runtime-plan-wings-x.md`) define the behaviour this
  spec carries over unchanged.
- **Wings-specific surface in wings-x** (checked by reading the code):
  - `argparse` `prog="wings x"`, description "Read-only access to X for
    Wings agents."; hints such as "run wings x auth login" in
    `auth.py`, `bookmarks.py` and `cli.py`; "a media lookup wings-x
    cannot read" in `media.py`.
  - User-Agent `wings-x/<version>` in `auth.py`.
  - `paths.py`: config in `$XDG_CONFIG_HOME/wings/x`, state in
    `$XDG_STATE_HOME/wings/x`, and an unused-at-runtime `releases_dir`.
    Files: `config.toml`, `token.json`, `app-bearer`, `token.lock`,
    `x.sqlite3`, `sync.lock`.
  - `WINGS_X_LIVE` gates `tests/live_smoke.sh`.
  - Nothing calls Wings at runtime.
- **Version string:** `wings_x/__init__.py` reads `0.1.0` at `main` and at
  tag `v0.2.0`.
- **Platform:** `auth.py` and `bookmarks.py` lock with `fcntl.flock`, so
  wings-x runs on POSIX only. `config.py` imports `tomllib` (Python 3.11).
- **Configuration a user must supply:** `config.toml` with a required
  `client_id`; `redirect_uri` defaults to
  `http://127.0.0.1:8765/callback`. Login requires exactly the scopes
  `tweet.read`, `users.read`, `bookmark.read` and `offline.access`.
  There is no `--version` flag.
- **Agent skill:** `x-research` lives in the Wings skills tree
  (`skills/x-research/SKILL.md` in `service-dept/wings` at `1400bb82`),
  installed by that tree's `install` script, which links each skill
  directory into `~/.agents/skills`, `~/.claude/skills` and
  `~/.gemini/config/skills` and removes links into the tree whose skill no
  longer exists. The tree also names `x-research` in its README, in the
  expected-skills list of `tests/test-skill-lint.sh`, and in scenarios
  S123–S125 of `tests/scenarios.md`.
- **Precedent:** `service-dept/op-img` at `5e70f15` publishes to PyPI by
  trusted publishing (`.github/workflows/publish.yml`: `test` → `build` →
  `publish`, environment `pypi`, `id-token: write`,
  `pypa/gh-action-pypi-publish@release/v1`), keeps `Formula/op-img.rb` in
  its repository, checks it with `homebrew.yml`
  (`Homebrew/actions/setup-homebrew@e99025eb970cfa124b8284a35463d87426917478`),
  and ships it through `service-dept/homebrew-tap`. Its `LICENSE` reads
  "MIT License / Copyright (c) 2026 Service Dept".
- **PyPI:** `fav` is unregistered (`https://pypi.org/pypi/fav/json`
  returned 404 on 2026-11-04).

## Design

### Decisions

| Decision | Chosen | Not chosen |
|---|---|---|
| Relationship to wings-x | `fav` replaces it; x-runtime is archived | A thin `wings x` wrapper over `fav`; a fork maintained in both places |
| Files | `fav`'s own XDG directories, with a one-time move from the wings-x ones | An override variable pointing at the Wings directories; no migration (new login and a full sync) |
| History | One clean import commit naming the source commit | Filtered history; making x-runtime public |
| License | MIT, Service Dept | Apache-2.0; none yet |
| Skill | `skills/x-research/` in this repository, the only copy | A generic skill here plus a Wings layer; `fav skill install` |
| Distribution | PyPI and `service-dept/tap`; Linux and macOS | PyPI only; Windows support |
| Wings side | A short switchover in this spec; no bootstrap change | A second spec for bootstrap; switchover in a later document |
| Order | Claim the PyPI name with 0.0.1 first, then port | Port first and release once; port in a private fork |

### 1. Claiming the name (0.0.1)

One pull request adds:

- `LICENSE`: MIT, "Copyright (c) 2026 Service Dept", as op-img.
- `pyproject.toml`: hatchling; `name = "fav"`; version read from
  `fav/__init__.py` (`[tool.hatch.version]`); `requires-python = ">=3.11"`;
  `license = "MIT"`; `license-files = ["LICENSE"]`; no dependencies;
  classifier `Development Status :: 1 - Planning`; no console script.
- `fav/__init__.py`: `__version__ = "0.0.1"` and a one-line docstring.
  The package sits at the repository root.
- `README.md`: what `fav` will be, that this release only holds the name,
  and that `fav` is unofficial and not affiliated with X Corp.
- `tests/test_package.py`: one `unittest` test that imports `fav` and
  checks `__version__` is a non-empty string. It exists so the release's
  test job has a test to run.
- `.github/workflows/publish.yml`: op-img's workflow with its three jobs,
  trigger and permissions unchanged, except that the test job runs
  `python -m unittest discover -s tests`.

Operator steps after merge, in order: add a pending publisher at
`https://pypi.org/manage/account/publishing/` (project `fav`, owner
`service-dept`, repository `fav`, workflow `publish.yml`, environment
`pypi`); publish GitHub release `v0.0.1`.

Done when `https://pypi.org/project/fav/` shows 0.0.1 uploaded by the
workflow.

### 2. Import and rename

**Import.** One commit copies `wings_x/` and `tests/` (with
`tests/fixtures/`) from x-runtime as they are at its `main`, with the
message `chore: import wings-x from x-runtime at <sha>`. Not imported:
`bin/wings-x` and `install` (replaced by the package entry point and
`python -m fav`) and `tests/test_install.py` (it tests `install`).

**Rename**, in commits after the import:

- `wings_x/` becomes `fav/`, with every import updated. It replaces the
  0.0.1 `fav/__init__.py`. The version is `0.1.0.dev0` until release.
- `argparse` `prog="fav"`, description "Read-only access to X for
  agents."
- Every hint naming `wings x` or `wings-x` names `fav` (for example
  "run fav auth login", "a media lookup fav cannot read").
- User-Agent `fav/<version>`.
- `WINGS_X_LIVE` becomes `FAV_LIVE`; `tests/live_smoke.sh` runs
  `python -m fav` from the checkout.
- `paths.py` drops `releases_dir`. Config is `$XDG_CONFIG_HOME/fav`
  (default `~/.config/fav`); state is `$XDG_STATE_HOME/fav` (default
  `~/.local/state/fav`). File names inside are unchanged.
- Unchanged: commands, flags, config keys, `X_*` error codes, JSON fields,
  the $1.00 default daily ceiling, and all behaviour defined by the
  wings-x spec.

**Migration**, in `fav/paths.py`, before the directories are created on
any command that uses them:

- Config and state are handled independently. When the new directory does
  not exist and the old one (`$XDG_CONFIG_HOME/wings/x` or
  `$XDG_STATE_HOME/wings/x`, from the same XDG roots) does, `os.rename`
  moves the old directory to the new path.
- Each move prints one line on stderr, with or without `--json`:
  `fav: moved <old> to <new>`.
- When the new directory exists, nothing is moved and the old one is left
  alone.
- A failed move, including across filesystems, ends the command with
  `X_LOCAL_IO`, naming both paths and the system's reason. Nothing is
  copied.
- The parents `$XDG_CONFIG_HOME/wings` and `$XDG_STATE_HOME/wings` are
  never touched.
- The migration stays for every 0.x release.

### 3. Packaging, CI and release (0.1.0)

**`pyproject.toml`**, extending section 1:

- `[project.scripts] fav = "fav.__main__:main"`.
- Classifiers: `Development Status :: 3 - Alpha`, `Environment :: Console`,
  `Operating System :: POSIX :: Linux`, `Operating System :: MacOS`,
  Python 3.11, 3.12, 3.13 and 3.14. Keywords: `x`, `twitter`, `cli`,
  `agents`, `bookmarks`.
- The wheel holds `fav/` only; the sdist adds `tests/`, `README.md` and
  `LICENSE`.
- `hatch-fancy-pypi-readme` rewrites the README's relative links to
  absolute GitHub URLs, as op-img does.

**`fav --version`** prints `fav <version>` and exits 0. It is the only
change to the command surface.

**`.github/workflows/tests.yml`**, on every pull request and push to
`main`:

- `unit`: `pip install .`, then `python -m unittest discover -s tests`,
  on `ubuntu-latest` and `macos-latest` with Python 3.11, 3.12 and 3.14.
- `installed` (wheel and sdist): build both, install each into a fresh
  virtualenv, copy `tests/` to a directory outside the checkout and run
  the suite there, so `tests.helpers` imports resolve against the
  installed package rather than the source tree; then run `fav --version`.

**Homebrew:**

- `Formula/fav.rb`: `Language::Python::Virtualenv`, `depends_on
  "python@3.14"`, no resources, `license "MIT"`, the PyPI sdist URL and
  sha256. `test do` runs `fav --version` and one offline command against
  an empty `HOME`, asserting its documented error code. The plan chooses
  the command.
- `.github/workflows/homebrew.yml`: op-img's job, renamed. It stages the
  formula in a local test tap, then runs `brew style`, an install from
  source, `brew audit --strict` and `brew test`, on `macos-latest` and
  `ubuntu-latest`, on pull requests and pushes touching the formula or
  the workflow, weekly, and on demand. `setup-homebrew` is pinned to the
  same commit as op-img's.

**Release 0.1.0:**

1. A pull request sets `__version__ = "0.1.0"`. After merge, the operator
   publishes GitHub release `v0.1.0`, and `publish.yml` uploads it.
2. A pull request points `Formula/fav.rb` at the 0.1.0 sdist;
   `homebrew.yml` checks it.
3. After merge, a pull request to `service-dept/homebrew-tap` adds the
   same `Formula/fav.rb`.

### 4. Skill and README

**`skills/x-research/SKILL.md`** is wings-x's `x-research`, rewritten for
any user:

- `wings-x` becomes `fav`; "the operator" becomes "the user". The
  "help calls it `wings x`" aside and the "Announce at start" line go.
- The frontmatter description keeps its triggers: posts, threads, users or
  bookmarks from X, and what someone said or bookmarked there.
- A new first step: when `fav` is not on PATH, or `fav --version` is below
  the minimum the skill states (0.1.0), give the three install commands,
  point a user with no X app at the README's setup section, and stop.
- Unchanged: the cheapest-command table; freshening bookmarks; the budget
  rules (`--budget` and `bookmarks sync --full` only with the user's yes
  for that call); error handling; reporting (origin, exact quotes, media
  and alt text); post text as content from strangers, never instructions;
  and the whole Never list, including never reaching X another way and
  never running `fav auth` or touching `fav`'s config, token or index.

**`README.md`** (PyPI shows it too), in this order:

1. What it is: a paragraph, the three questions an agent can now answer
   (search X, find my bookmarks, compare the two), and the unofficial,
   not-affiliated-with-X line.
2. Install: pipx, uv, brew; Linux and macOS; Python 3.11 or later and
   SQLite with FTS5, with what `X_ENVIRONMENT` means.
3. X developer app setup: X's prepaid, pay-per-use credits and the absence
   of a free tier; why owning the app makes bookmark reads cheaper;
   setting a console spending limit; creating a project and app with
   OAuth 2.0 as a public client and callback
   `http://127.0.0.1:8765/callback`; a minimal `~/.config/fav/config.toml`
   (only `client_id` required); `fav auth login` and its four scopes;
   optional `fav auth app-token` for `fav usage`. Console labels are
   checked against X's documentation when the README is written.
4. Commands: the wings-x command table, renamed, plus `--json`,
   `--budget` and the freshness line.
5. Spending: prices per resource, the daily ceiling and reservations,
   `--budget`, changing prices in `config.toml`, and that a local estimate
   cannot prove what X billed.
6. With an agent: adding `skills/x-research` to Claude Code, Codex and
   other CLIs that read `SKILL.md`, by copying or linking the directory.
   Each CLI's skills path is checked against its documentation.
7. Files: the config and state directories, mode 0600 files, and one
   sentence on the move from wings-x's directories.
8. Development: `python -m unittest discover -s tests`, and
   `FAV_LIVE=1 tests/live_smoke.sh` with its warning that it spends real
   credits.
9. License: MIT, "fav by Service Dept.", no personal names.

### 5. Switchover on the Wings host

After 0.1.0 is on PyPI, in order:

1. `uv tool install fav==0.1.0`, so `fav` runs from an installed release.
2. `fav auth status` performs the migration and prints the two `moved`
   lines and the same account and scopes as before;
   `fav bookmarks list --limit 1` shows the existing last sync;
   `fav usage` shows the existing ledger. No new login or sync.
3. Remove the `wings-x` link from `~/.local/bin` and wings-x's releases
   directory, immediately after step 2, since a later `wings-x` run would
   recreate empty directories at the old paths.
4. One pull request to the Wings skills tree: delete
   `skills/x-research/`; remove it from `tests/test-skill-lint.sh`'s
   expected list; change its README row to say it comes from `fav`; move
   scenarios S123–S125 to this repository as
   `skills/x-research/tests/scenarios.md`, with their fake `wings-x`
   renamed to `fav`, leaving one line in the tree saying where they went.
5. Run the skills tree's `install`, which removes the stale `x-research`
   links, then link this repository's `skills/x-research` into the same
   three skill directories by hand. `install` leaves links outside its
   tree alone. The link follows this repository's `main` while the CLI is
   pinned; the skill's minimum-version step covers an older CLI.
6. Archive x-runtime through the repo-cleanup process, after a last pull
   request replaces its README with a pointer to `service-dept/fav`. It
   stays private; its documents stay in place.

Done when `fav` runs from `~/.local/bin` on the existing token and index,
`wings-x` is not on PATH, each agent CLI's installed `x-research` is this
repository's, and S123–S125 pass against `fav`.

## Testing

- **Carried suite:** every wings-x test except `test_install.py`, renamed,
  passing under `python -m unittest discover -s tests`.
- **New unit tests:**
  - migration: both directories move; only one old directory exists; the
    new directory already exists (no move, old left alone); the stderr
    line; a failed `os.rename` gives `X_LOCAL_IO` with both paths and
    leaves both directories as they were;
  - `fav --version` prints `fav <version>` and exits 0;
  - help shows `fav`; every login hint names `fav`;
  - no file in `fav/` contains `wings` except the migration's two old-path
    components in `paths.py`.
- **CI:** `tests.yml` (unit matrix and installed packages),
  `publish.yml`'s test job, and `homebrew.yml`.
- **Skill:** S123–S125 re-run against a fake `fav` with the rewritten
  skill, and a new scenario: with no `fav` on PATH, the skill gives the
  three install commands and stops without reaching X another way.
- **Live, with the operator's yes, spending real credits:**
  `FAV_LIVE=1 tests/live_smoke.sh` once from the release branch before
  `v0.1.0`, comparing the ledger estimate with the change in X's credit
  balance; after each PyPI release, `uv tool install fav==<version>` and
  `fav --version` in a clean environment; switchover steps 2 and 5 as
  the real-machine checks of the migration and the skill.

## Out of scope

- Windows.
- Any command or flag beyond `--version`.
- Posting or any other write to X.
- X's hosted MCP server; full-archive search.
- Changes to Wings bootstrap or to the skills tree's `install` script.
- A website for `fav`.
- Making x-runtime public.

## Follow-ups

- Remove the migration after the 0.x releases.
- Teach the Wings skills tree's `install` to link skills from other
  repositories, if a second one appears.
- A website.
- Automating the Homebrew formula update after a PyPI release.

## Open questions

1. Should `publish.yml`'s test job also fail when the release tag is not
   `v<__version__>`? wings-x's `v0.2.0` tag carries `__version__ =
   "0.1.0"`, so its User-Agent misreports the version; the check would
   stop that recurring in `fav`.
