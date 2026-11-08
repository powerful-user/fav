---
title: "fav: wings-x as a standalone package and skill"
kind: spec
target: fav
topic: fav-standalone
status: draft
written: 2026-11-04T22:04:49Z
by: claude
worktree: /home/jmo/linda/repos/fav/.worktrees/spec-fav-standalone
approved_at: 2026-11-05T22:52:45Z
approved_by: josh
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
  Package `wings_x/` (16 modules), a development launcher `bin/wings-x`
  (`exec python3 -B -P -m wings_x` with the checkout on `PYTHONPATH`), an
  `./install <version>` script that exports a tag into
  `$XDG_DATA_HOME/wings-x/releases/<version>/` and links
  `~/.local/bin/wings-x`, and `tests/` (a package with `__init__.py`,
  `helpers.py`, `fixtures/` and `live_smoke.sh`). Its documented test
  command is `python3 -B -m unittest discover -s tests -t .` from the
  repository root; test modules import `tests.helpers`, which finds
  fixtures relative to its own file. Its approved spec and plan
  (`docs/2026-10-02-x-runtime-spec-wings-x.md`,
  `docs/2026-10-02-x-runtime-plan-wings-x.md`) define the behaviour this
  spec carries over unchanged.
- **Wings-specific surface in wings-x** (checked by reading the code):
  - `cli.py`: `argparse` `prog="wings x"`, description "Read-only access
    to X for Wings agents.", and messages naming `wings x` (for example
    line 151, "this is not the login wings x is waiting for"). Hints such
    as "run wings x auth login" also appear in `auth.py` and
    `bookmarks.py`; `media.py` says "a media lookup wings-x cannot read";
    `commands.py` has "X returned a response wings-x cannot read" and a
    comment naming `wings_x.bookmarks` (line 520).
  - User-Agent `wings-x/<version>` in both `auth.py` and `client.py`
    (`USER_AGENT`, line 21).
  - `paths.py`: config in `$XDG_CONFIG_HOME/wings/x`, state in
    `$XDG_STATE_HOME/wings/x`, and `releases_dir`, used only by
    `tests/test_paths.py`. Files: `config.toml`, `token.json`,
    `app-bearer`, `token.lock`, `x.sqlite3` (WAL mode, so also `-wal` and
    `-shm`), `sync.lock`. `commands.py` creates both directories
    (`ensure_dirs`, line 49) before loading the config, on every command
    that builds a context.
  - Tests encode the old names: `tests/helpers.py` `config_dir` returns
    `.../wings/x` and `TempHome` uses the prefix `wings-x-test-`;
    `test_secrets.py` line 44 and `test_bookmarks_sync.py` line 138 build
    `wings/x` paths; `test_paths.py` asserts the old paths and
    `releases_dir`; `test_cli.py` `LauncherTest` runs `bin/wings-x`
    directly and through a symlink; assertions expect `usage: wings x`
    (`test_cli.py` line 51), a `wings-x/` User-Agent (`test_client.py`
    line 48) and "wings-x cannot read" (`test_commands.py` line 129); 11
    `mock.patch("wings_x....")` strings; `test_secrets.py` runs
    `tests/live_smoke.sh`, which is gated by `WINGS_X_LIVE`. Outside
    import lines and `mock.patch` targets, 54 lines under `wings_x/` and
    `tests/` mention `wings` in some case.
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
  (`skills/x-research/SKILL.md` in `service-dept/wings` at `1400bb82`). Its
  step 1 says "repeating a call that reaches X pays again"; X documents
  deduplication of resources within a UTC day, and wings-x's ledger does
  not charge a resource it has already recorded that day. The tree's
  `install` script links each skill directory into `~/.agents/skills`,
  `~/.claude/skills` and `~/.gemini/config/skills`. On the Wings host, no
  `x-research` link exists in any of the three. The existing links point
  through `~/linda/repos/skills`, a symlink to the tree, while `install`
  resolves its own root with `pwd -P` to the physical path, so it reports
  each existing link as a conflict. The tree also names `x-research` in
  its README, in the expected-skills list of `tests/test-skill-lint.sh`,
  and in scenarios S123–S125 of `tests/scenarios.md`.
- **Precedent:** `service-dept/op-img` at `5e70f15` publishes to PyPI by
  trusted publishing (`.github/workflows/publish.yml`: `test` → `build` →
  `publish`, environment `pypi`, `id-token: write`,
  `pypa/gh-action-pypi-publish@release/v1`), keeps `Formula/op-img.rb` in
  its repository, checks it with `homebrew.yml`
  (`Homebrew/actions/setup-homebrew@e99025eb970cfa124b8284a35463d87426917478`),
  and ships it through `service-dept/homebrew-tap`. Its `LICENSE` reads
  "MIT License / Copyright (c) 2026 Service Dept". Its `pyproject.toml`
  uses hatchling with `dynamic = ["version", "readme"]`,
  `[tool.hatch.version]`, and `hatch-fancy-pypi-readme` substitutions.
- **PyPI** (docs.pypi.org, read 2026-11-04): `fav` is unregistered
  (`https://pypi.org/pypi/fav/json` returned 404). A pending publisher
  "does not create a project or reserve a project's name until it is
  actually used to publish". The name-retention policy lists as invalid a
  project that "is name squatting (package has no functionality or is
  empty)", and says such projects "will be removed from the Index".

## Design

### Decisions

| Decision | Chosen | Not chosen |
|---|---|---|
| Relationship to wings-x | `fav` replaces it; x-runtime is archived | A thin `wings x` wrapper over `fav`; a fork maintained in both places |
| Files | `fav`'s own XDG directories; on the Wings host the wings-x directories are moved by hand at switchover | A first-run migration inside `fav` (it would only ever serve the one host, x-runtime being private); an override variable pointing at the Wings directories; no move (a new login and a full sync) |
| History | One clean import commit naming the source commit | Filtered history; making x-runtime public |
| License | MIT, Service Dept | Apache-2.0; none yet |
| Skill | `skills/x-research/` in this repository, the only copy | A generic skill here plus a Wings layer; `fav skill install` |
| Distribution | PyPI and `service-dept/tap`; Linux and macOS | PyPI only; Windows support |
| Wings side | A short switchover in this spec; no bootstrap change | A second spec for bootstrap; switchover in a later document |
| Order | Port first, then claim the PyPI name with a working `0.1.0a1`, then finish and release `0.1.0` | An empty `0.0.1` placeholder (PyPI removes name-squatting packages); one release at `0.1.0`; a private fork |

### 1. Import and rename

**Repository basics**, in the first pull request with the import:
`LICENSE` (MIT, "Copyright (c) 2026 Service Dept", as op-img).

**Import.** One commit copies `wings_x/` and `tests/` (including
`__init__.py`, `helpers.py`, `fixtures/` and `live_smoke.sh`) from
x-runtime as they are at its `main`, with the message
`chore: import wings-x from x-runtime at <sha>`. Not imported:
`bin/wings-x` and `install` (replaced by the package entry point and
`python -m fav`) and `tests/test_install.py` (it tests `install`).

**Rename**, in commits after the import. The rule: after the rename, no
line under `fav/` or `tests/` mentions `wings` in any case, including
comments, temporary-directory prefixes, test names and assertions. In
particular:

- `wings_x/` becomes `fav/`, with every import and every
  `mock.patch("wings_x....")` target updated. `__version__` is
  `0.1.0a1`.
- `cli.py`: `prog="fav"`, description "Read-only access to X for agents."
- Every message naming `wings x` or `wings-x`, in any module, names `fav`
  (for example "run fav auth login", "this is not the login fav is
  waiting for", "a media lookup fav cannot read", "X returned a response
  fav cannot read"), and the assertions expecting them change with them.
- Both User-Agents (`auth.py` and `client.py`) become `fav/<version>`.
- `WINGS_X_LIVE` becomes `FAV_LIVE`; `tests/live_smoke.sh` runs
  `python3 -B -m fav` with the checkout on `PYTHONPATH`.
- `paths.py` drops `releases_dir`. Config is `$XDG_CONFIG_HOME/fav`
  (default `~/.config/fav`); state is `$XDG_STATE_HOME/fav` (default
  `~/.local/state/fav`). File names inside are unchanged. `fav` never
  reads or moves the wings-x directories.
- Tests: `tests/helpers.py` (`config_dir`, the `TempHome` prefix),
  `test_secrets.py`, `test_bookmarks_sync.py` and `test_paths.py` use the
  new names, and `test_paths.py` no longer mentions `releases_dir`.
  `LauncherTest` in `test_cli.py` is replaced by one test that runs
  `sys.executable -B -m fav --help` with the repository root on
  `PYTHONPATH` and an empty `HOME`, and expects exit 0 and `usage: fav`.
  The symlink test is deleted, since there is no launcher script.
- Unchanged: commands, flags, config keys, `X_*` error codes, JSON fields,
  the $1.00 default daily ceiling, and all behaviour defined by the
  wings-x spec.

### 2. Packaging and CI

**`pyproject.toml`**, modelled on op-img's:

- `[build-system]` hatchling and `hatch-fancy-pypi-readme`.
- `name = "fav"`; `dynamic = ["version", "readme"]` with
  `[tool.hatch.version] path = "fav/__init__.py"`;
  `requires-python = ">=3.11"`; `license = "MIT"`;
  `license-files = ["LICENSE"]`; no dependencies.
- `[project.scripts] fav = "fav.__main__:main"`.
- `[project.urls]` `Homepage`, `Repository` and `Issues` on
  `https://github.com/service-dept/fav`.
- Classifiers: `Development Status :: 3 - Alpha`, `Environment :: Console`,
  `Operating System :: POSIX :: Linux`, `Operating System :: MacOS`,
  Python 3.11, 3.12, 3.13 and 3.14. Keywords: `x`, `twitter`, `cli`,
  `agents`, `bookmarks`.
- `[tool.hatch.build.targets.wheel] include = ["fav"]`;
  `[tool.hatch.build.targets.sdist] include = ["fav", "tests", "README.md",
  "LICENSE"]`.
- `hatch-fancy-pypi-readme`: the fragment is `README.md`; substitutions
  turn its relative links (`](LICENSE)` and `](skills/...)`) into
  `https://github.com/service-dept/fav/blob/main/...` URLs.

**`fav --version`** prints `fav <version>` and exits 0 without creating
any directory. It is the only change to the command surface.

**`.github/workflows/tests.yml`**, on every pull request and push to
`main`:

- `unit`, on `ubuntu-latest` and `macos-latest` with Python 3.11, 3.12,
  3.13 and 3.14: from the repository root, with no install,
  `python -B -m unittest discover -s tests -t .`. This tests the source
  tree.
- `installed`, once for the wheel and once for the sdist, on
  `ubuntu-latest` with Python 3.12: `pip install build`,
  `python -m build`, create a virtualenv in `$RUNNER_TEMP/venv`, install
  the built file into it, copy the whole `tests/` directory to
  `$RUNNER_TEMP/suite/tests`, and from `$RUNNER_TEMP/suite` run
  `$RUNNER_TEMP/venv/bin/python -B -m unittest discover -s tests -t .`,
  so `fav` imports from the installed package. Then
  `$RUNNER_TEMP/venv/bin/fav --version`.

**`.github/workflows/publish.yml`**: op-img's workflow, with its three
jobs, its `release: published` trigger and its permissions unchanged,
except that the test job runs
`python -B -m unittest discover -s tests -t .` from the repository root
in place of pytest, and first checks that the release tag matches the
package version, so a release can never misreport its version the way
wings-x's `v0.2.0` (carrying `__version__ = "0.1.0"`) does:

```yaml
- name: Tag matches version
  env:
    TAG: ${{ github.event.release.tag_name }}
  run: test "$TAG" = "v$(python -c 'import fav; print(fav.__version__)')"
```

A mismatch fails the test job, so `build` and `publish` never run.

Before the first release, nothing on the Wings host runs a `fav`
command other than `--help`, `--version` and the test suite (which uses
temporary homes), so `~/.config/fav` and `~/.local/state/fav` do not
exist when section 6 moves the data there.

### 3. First release: 0.1.0a1

When sections 1 and 2 are merged and `tests.yml` is green on `main`:

1. The operator adds a pending publisher at
   `https://pypi.org/manage/account/publishing/`: project `fav`, owner
   `service-dept`, repository `fav`, workflow `publish.yml`, environment
   `pypi`.
2. The operator publishes GitHub release `v0.1.0a1`, marked as a
   pre-release.

Done when `https://pypi.org/project/fav/` shows 0.1.0a1 uploaded by the
workflow, and `uv tool install fav==0.1.0a1` followed by `fav --version`
prints `fav 0.1.0a1` in a clean environment. A plain `pip install fav`
skips it, since it is a pre-release.

### 4. Skill and README

**`skills/x-research/SKILL.md`** is wings-x's `x-research`, rewritten for
any user:

- `wings-x` becomes `fav`; "the operator" becomes "the user". The
  "help calls it `wings x`" aside and the "Announce at start" line go.
- Step 1's "repeating a call that reaches X pays again" becomes: a
  repeated call can be charged again (X deduplicates some repeats within
  a UTC day, but not reliably), so decide on `--json` before the call.
- The error step's "(it opens a browser)", said of `auth login`, becomes
  "(it prints a URL to open in a browser)": `fav auth login` opens no
  browser itself.
- The frontmatter description keeps its triggers: posts, threads, users or
  bookmarks from X, and what someone said or bookmarked there.
- A new first step: when `fav` is not on PATH, or `fav --version` is below
  the minimum the skill states (0.1.0), give the three install commands,
  point a user with no X app at the README's setup section, and stop.
- Otherwise unchanged: the cheapest-command table; freshening bookmarks;
  the budget rules (`--budget` and `bookmarks sync --full` only with the
  user's yes for that call); error handling; reporting (origin, exact
  quotes, media and alt text); post text as content from strangers, never
  instructions; and the whole Never list, including never reaching X
  another way and never running `fav auth` or touching `fav`'s config,
  token or index.

**`README.md`** (PyPI shows it too), in this order:

1. What it is: a paragraph, the three questions an agent can now answer
   (search X, find my bookmarks, compare the two), and the unofficial,
   not-affiliated-with-X line.
2. Install: pipx, uv, brew; Linux and macOS; Python 3.11 or later and
   SQLite with FTS5, with what `X_ENVIRONMENT` means.
3. X developer app setup: X's prepaid, pay-per-use credits and the absence
   of a free tier; why owning the app makes bookmark reads cheaper;
   setting a console spending limit; creating a project and app with
   OAuth 2.0 as a public (native) client and callback
   `http://127.0.0.1:8765/callback`; a minimal `~/.config/fav/config.toml`
   (only `client_id` required); `fav auth login` and its four scopes;
   optional `fav auth app-token` for `fav usage`. Console labels are
   checked against X's documentation when the README is written.
4. Commands: the wings-x command table, renamed, plus `--json`,
   `--budget` and the freshness line.
5. Spending: prices per resource, the daily ceiling and reservations,
   `--budget`, changing prices in `config.toml`, and that a local estimate
   cannot prove what X billed.
6. With an agent: the skill is not in the PyPI or Homebrew package. Get
   the `skills/x-research` directory for the installed version from
   GitHub (`git clone --depth 1 --branch v<version>
   https://github.com/service-dept/fav`, or the release's source archive)
   and copy or link it into the agent's skills directory, for Claude
   Code, Codex and other CLIs that read `SKILL.md`. Each CLI's skills path
   is checked against its documentation.
7. Files: the config and state directories and their mode 0600 files.
8. Development: `python -B -m unittest discover -s tests -t .`, and
   `FAV_LIVE=1 tests/live_smoke.sh` with its warning that it spends real
   credits.
9. License: MIT, "fav by Service Dept.", no personal names.

### 5. Homebrew and release 0.1.0

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
  same commit as op-img's. If `brew audit --strict` rejects a formula with
  no resources, the formula changes to satisfy it; the tap waits for a
  green run.

**Release 0.1.0**, after section 4 is merged and section 6's steps 1–3
are done:

1. A pull request sets `__version__ = "0.1.0"`. After merge, the operator
   publishes GitHub release `v0.1.0`, and `publish.yml` uploads it.
2. A pull request points `Formula/fav.rb` at the 0.1.0 sdist;
   `homebrew.yml` checks it.
3. After merge, a pull request to `service-dept/homebrew-tap` adds the
   same `Formula/fav.rb`.

### 6. Switchover on the Wings host

Steps 1–3 happen once 0.1.0a1 is on PyPI and before `v0.1.0`, so the live
check runs against the real, moved data; steps 4–7 follow 0.1.0.

1. **Install the alpha:** `uv tool install fav==0.1.0a1`; `fav --version`
   prints `fav 0.1.0a1` and creates no directory (section 2). wings-x is
   still installed and untouched, so a failed install changes nothing.
2. **Move the data, check it, then retire wings-x**, in one sitting:
   - Confirm no wings-x process is running: `pgrep -fa wings_x` prints
     nothing. A wings-x refresh or sync in flight while its directory
     moves can lose a rotated token or the database's WAL files.
   - Confirm `~/.config/fav` and `~/.local/state/fav` do not exist, then
     `mv ~/.config/wings/x ~/.config/fav` and
     `mv ~/.local/state/wings/x ~/.local/state/fav`. `~/.config/wings/`
     and `~/.local/state/wings/` stay; other Wings services use them.
   - Check: `fav auth status` shows the same account and scopes as
     before; `fav bookmarks list --limit 1` shows the existing last sync;
     `fav usage` shows the existing ledger. No new login or sync.
   - Only when every check passes, remove the `wings-x` link from
     `~/.local/bin` and `~/.local/share/wings-x/`, since a later `wings-x`
     run would create empty directories at the old paths and ask for a
     new login. If a check fails, roll back with the two `mv` commands
     reversed (the old paths are free again, since wings-x has not run),
     and wings-x works as before.
3. **Live check:** with the operator's yes, from the release branch,
   `FAV_LIVE=1 tests/live_smoke.sh`, comparing the ledger estimate with
   the change in X's credit balance.
4. After 0.1.0 is on PyPI: `uv tool install fav==0.1.0`, so `fav` runs
   from an installed release, and `fav --version` prints `fav 0.1.0`.
5. One pull request to the Wings skills tree: delete
   `skills/x-research/`; remove it from `tests/test-skill-lint.sh`'s
   expected list; change its README row to say it comes from `fav`; move
   scenarios S123–S125 to this repository as
   `skills/x-research/tests/scenarios.md`, with their fake `wings-x`
   renamed to `fav`, leaving one line in the tree saying where they went.
6. Link the skill by hand, without relying on the tree's `install`: for
   each of `~/.agents/skills`, `~/.claude/skills` and
   `~/.gemini/config/skills`, create `x-research` as a symlink to
   `/home/jmo/linda/repos/fav/skills/x-research`. No `x-research` link
   exists there today, so nothing needs removing. The link serves
   whatever that checkout has checked out, so the checkout stays on
   `main`; the skill's minimum-version step covers a pinned CLI older than
   the skill.
7. Archive x-runtime through the repo-cleanup process, after a last pull
   request replaces its README with a pointer to `service-dept/fav`. It
   stays private; its documents stay in place.

Done when `fav` 0.1.0 runs from `~/.local/bin` on the existing token and
index, `wings-x` is not on PATH, each of the three skill directories holds
an `x-research` link to this repository's skill, and S123–S125 pass
against `fav`.

## Testing

- **Carried suite:** every wings-x test except `test_install.py`, renamed
  as section 1 says, passing under
  `python -B -m unittest discover -s tests -t .`.
- **New unit tests:**
  - `fav --version` prints `fav <version>`, exits 0, and creates no
    directory under an empty `HOME`;
  - `python -m fav --help` shows `usage: fav` (the replacement for
    `LauncherTest`); every login hint names `fav`;
  - no file under `fav/` or `tests/` contains `wings`, case-insensitively,
    apart from this test's own pattern.
- **CI:** `tests.yml` (the unit matrix and the installed packages),
  `publish.yml`'s test job with its tag check, and `homebrew.yml`,
  including `brew audit --strict`.
- **Skill:** S123–S125 re-run against a fake `fav` with the rewritten
  skill, and a new scenario: with no `fav` on PATH, the skill gives the
  three install commands and stops without reaching X another way.
- **Releases:** after each PyPI release (0.1.0a1 and 0.1.0),
  `uv tool install fav==<version>` and `fav --version` in a clean
  environment.
- **Live, with the operator's yes, spending real credits:** section 6
  step 3 before `v0.1.0`; section 6 steps 2 and 6 as the real-machine
  checks of the moved data and the skill.

## Out of scope

- Windows.
- Any command or flag beyond `--version`.
- Posting or any other write to X.
- X's hosted MCP server; full-archive search.
- Moving wings-x data on any machine other than the Wings host.
- Changes to Wings bootstrap or to the skills tree's `install` script.
- A website for `fav`.
- Making x-runtime public.

## Follow-ups

- The skills tree's `install` reports every existing link as a conflict
  when the links point through a symlink to the tree: already open as
  `service-dept/wings#209`.
- Teach that `install` to link skills from other repositories, if a
  second one appears.
- A website.
- Automating the Homebrew formula update after a PyPI release.

## Open questions

- none. The one open question, whether `publish.yml` fails when the
  release tag is not `v<__version__>`, was answered yes on 2026-11-05;
  section 2 has the check.
