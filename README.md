# fav

fav is a read-only command line for X, built for agents. It searches recent posts, keeps a local full-text index of your bookmarks, and estimates the cost of a call before it spends.

An agent can answer three questions with it: what people are saying on X, what you have bookmarked, and how those two differ.

fav is unofficial and not affiliated with X.

## Install

fav runs on Linux and macOS. It needs Python 3.11 or later and SQLite with FTS5. If either is missing, fav exits with `X_ENVIRONMENT` and one of these messages: `Python 3.11 or later is required`, or `SQLite lacks FTS5`.

```
pipx install fav
uv tool install fav
brew install service-dept/tap/fav
```

## Set up your X app

X's API is pay-per-usage. You buy credits in the [Developer Console](https://console.x.com); there is no subscription. Set a spending limit there (the console label is **Spending limit**): a maximum dollar amount per billing cycle. When that limit is reached, X blocks API requests until the next cycle. See [Pricing](https://docs.x.com/x-api/getting-started/pricing.md) and the [Developer Console](https://docs.x.com/fundamentals/developer-portal.md) pages.

Bookmark reads are cheaper when the app is yours. X prices an Owned Read, including `GET /2/users/{id}/bookmarks` for the owner of the developer app, at $0.001 per resource. A post read is $0.005. That is why fav's bookmark sync uses the owned-read price.

Create an app in the Developer Console. The console manages projects and apps. The [Apps](https://docs.x.com/fundamentals/developer-apps.md) page says **Create App**; the [Developer Console](https://docs.x.com/fundamentals/developer-portal.md) getting-started steps say **New App**. Choose OAuth 2.0. Under **OAuth 2.0 app types**, choose **Native App**. That type is a public client: PKCE, no client secret. fav only needs the **Client ID**.

Allowlist this callback URL on the app. X calls these callback URLs (redirect URIs), and the URL must match exactly, including the scheme. For local development X says to use `http://127.0.0.1`, not `localhost`:

```
http://127.0.0.1:8765/callback
```

That is fav's default. To use another callback, set `redirect_uri` in the config file to the same URL you allowlisted.

Write `~/.config/fav/config.toml` with the Client ID. `client_id` is the only required key:

```toml
client_id = "your client id"
```

Then sign in. This opens a browser and asks X for exactly these four scopes: `tweet.read`, `users.read`, `bookmark.read`, `offline.access`.

```
fav auth login
```

`fav usage` can also show X's credit balance and post counts. That needs the app's bearer token, which the Apps page lists as **Bearer Token**:

```
fav auth app-token
```

## Commands

`fav <command>`. Plain text by default. `--json` prints one JSON document on stdout. A command that reads posts, users or bookmarks from X also takes `--budget USD`, which allows extra spend for that invocation above the daily ceiling. `auth` and `usage` do not take `--budget`.

`bookmarks list` and `bookmarks search` never call X. They always print when the index was last synced (`last sync 3h ago`; `last_sync` in JSON).

| Command | Calls X | What it does |
|---|---|---|
| `fav auth login` | yes | PKCE login in a browser |
| `fav auth app-token` | no | Store the app bearer token used by `usage` |
| `fav auth status` | yes | Signed-in account, scopes, token expiry, whether an app token is stored |
| `fav auth logout` | yes | Revoke the user token at X and delete it locally |
| `fav search "<query>" [--since 3d\|DATE] [--until DATE] [--from @user] [--lang CODE] [--limit N] [--next TOKEN]` | yes | Recent search, last 7 days. `--limit` is 10 to 100, default 25. One page per call; prints the next token when more exist |
| `fav post <url\|id>` | yes | One post, plus the posts it quotes, replies to or reposts |
| `fav thread <url\|id> [--limit N]` | yes | The conversation around a post. `--limit` default 50, counting the post, its ancestors and the replies |
| `fav user <@name\|id>` | yes | Look up a user |
| `fav bookmarks sync [--full]` | yes | Fetch bookmarks into the local index. `--full` also finds removals and refreshes folders |
| `fav bookmarks list [--folder ID] [--limit N] [--offset N] [--include-removed]` | no | Local index, newest first |
| `fav bookmarks search "<query>" [--from @user] [--since DATE] [--until DATE] [--folder ID] [--limit N] [--include-removed]` | no | Local full-text search (phrases, `OR`, `prefix*`) |
| `fav bookmarks folders` | yes | Fetch the folder list from X and store it |
| `fav usage [--days N]` | yes | Local ledger by UTC day, beside X's credit balance and post counts when an app token is stored |

## Spending

fav prices each resource from `config.toml`, or from these defaults when a price is unset:

| Key | Default | What fav counts |
|---|---|---|
| `post_read` | $0.005 | A post read from search, `post`, `thread` or a media lookup |
| `owned_read` | $0.001 | A bookmark or other owned resource |
| `user_read` | $0.010 | A user lookup |
| `media_read` | $0.005 | One attachment on a media lookup |

The daily ceiling is `daily_ceiling_usd`, $1.00 by default, per UTC day. Before a request, fav reserves that request's worst case against today's ceiling and any `--budget` you passed. If the worst case does not fit, fav stops with `X_BUDGET_EXCEEDED` and tells you the allowance. `--budget USD` raises the allowance for that invocation only. Pass it only when you mean that call to spend more.

Change the ceiling or the prices in `~/.config/fav/config.toml`:

```toml
daily_ceiling_usd = 1.0

[prices]
post_read = 0.005
owned_read = 0.001
user_read = 0.010
media_read = 0.005
```

The figure fav prints is its own estimate. X documents that the same resource requested twice in 24 hours is charged once, and prices can change. A local estimate cannot prove what X billed. `fav usage` shows the estimate beside X's own figures.

## With an agent

The skill is not part of the PyPI package or the Homebrew formula. Get `skills/x-research` for the version you installed, from the git tag or from that release's source archive:

```
git clone --depth 1 --branch v<version> https://github.com/service-dept/fav
```

The instructions are in [skills/x-research/SKILL.md](skills/x-research/SKILL.md). Copy or link that directory into the skills directory your agent reads.

Claude Code reads a personal skill from `~/.claude/skills/x-research/SKILL.md` ([Claude Code skills](https://code.claude.com/docs/en/skills)). Codex reads a personal skill from `$HOME/.agents/skills/x-research/SKILL.md` ([Codex skills](https://developers.openai.com/codex/skills)).

## Files

fav keeps secrets and the bookmark index in two directories, created mode `0700`. The files inside are mode `0600`.

| Directory | Default | Files |
|---|---|---|
| Config | `~/.config/fav` (`$XDG_CONFIG_HOME/fav` when that variable is set) | `config.toml`, `token.json`, `app-bearer` |
| State | `~/.local/state/fav` (`$XDG_STATE_HOME/fav` when that variable is set) | `x.sqlite3` (posts, bookmarks, the spend ledger) |

## Development

From the repository root:

```
python -B -m unittest discover -s tests -t .
```

`tests/live_smoke.sh` calls the real X API (search, one post, a bookmark sync, usage). It spends real credits. It does nothing unless you set `FAV_LIVE=1`, and it asks you to compare fav's ledger estimate with the change in the X credit balance:

```
FAV_LIVE=1 tests/live_smoke.sh
```

## License

fav by Service Dept. MIT. See [LICENSE](LICENSE).
