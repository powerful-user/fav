import argparse
import contextlib
import getpass
import hmac
import http.server
import math
import os
import queue
import socketserver
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

from fav import auth as auth_mod
from fav import bookmarks as bookmarks_mod
from fav import commands
from fav import config as config_mod
from fav import output
from fav.client import UrllibTransport
from fav.errors import EXIT_ERROR, EXIT_OK, EXIT_USAGE, XError

LOOPBACK_HOST = "127.0.0.1"
FIRST_SEEN = ("first_seen_at is when sync first saw a bookmark, not when it was bookmarked; "
              "X does not report that.")


def _budget(text):
    try:
        value = float(text)
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError("--budget takes an amount in USD, such as 0.50") from None
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("--budget must be a finite amount of at least 0")
    return value


def _parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print one JSON document on stdout")
    spending = argparse.ArgumentParser(add_help=False)
    spending.add_argument("--budget", type=_budget, metavar="USD",
                          help="extra spend allowed for this invocation above the daily ceiling")

    parser = argparse.ArgumentParser(prog="fav", description="Read-only access to X for agents.")
    sub = parser.add_subparsers(dest="command", metavar="command", required=True)

    auth = sub.add_parser("auth", help="sign in, check, or sign out")
    auth_sub = auth.add_subparsers(dest="auth_command", metavar="auth_command", required=True)
    auth_sub.add_parser("login", parents=[common], help="sign in to X with PKCE")
    auth_sub.add_parser("status", parents=[common], help="show the signed-in account, scopes and expiry")
    auth_sub.add_parser("logout", parents=[common], help="revoke the user token and delete it")
    auth_sub.add_parser("app-token", parents=[common], help="store the app bearer token used by usage")

    search = sub.add_parser("search", parents=[common, spending], help="search recent posts (last 7 days)")
    search.add_argument("query")
    search.add_argument("--since", help="duration such as 3d or 12h, or an ISO date")
    search.add_argument("--until", help="ISO date")
    search.add_argument("--from", dest="from_user", metavar="@USER", help="only posts by this user")
    search.add_argument("--lang", metavar="CODE", help="only posts in this language")
    search.add_argument("--limit", type=int, metavar="N", help="posts to return, 10-100 (default 25)")
    search.add_argument("--next", dest="next_token", metavar="TOKEN", help="next page token from a previous call")

    post = sub.add_parser("post", parents=[common, spending], help="one post with its referenced posts")
    post.add_argument("ref", metavar="url|id")

    thread = sub.add_parser("thread", parents=[common, spending], help="the conversation around a post")
    thread.add_argument("ref", metavar="url|id")
    thread.add_argument("--limit", type=int, metavar="N", help="total posts to return (default 50)")

    user = sub.add_parser("user", parents=[common, spending], help="look up a user")
    user.add_argument("ref", metavar="@name|id")

    bookmarks = sub.add_parser("bookmarks", help="sync, list and search the local bookmark index")
    bm_sub = bookmarks.add_subparsers(dest="bookmarks_command", metavar="bookmarks_command", required=True)
    bm_sync = bm_sub.add_parser("sync", parents=[common, spending], help="fetch bookmarks from X into the index")
    bm_sync.add_argument("--full", action="store_true",
                         help="page through every bookmark X returns, mark missing ones, and sync folders")
    bm_list = bm_sub.add_parser("list", parents=[common], help="bookmarks from the local index, newest first",
                                description="Bookmarks from the local index, newest first, without calling X. "
                                + FIRST_SEEN)
    bm_list.add_argument("--folder", metavar="ID", help="only bookmarks in this folder")
    bm_list.add_argument("--limit", type=int, metavar="N", help="bookmarks to return, 1-500 (default 25)")
    bm_list.add_argument("--offset", type=int, default=0, metavar="N", help="bookmarks to skip (default 0)")
    bm_list.add_argument("--include-removed", action="store_true", help="also show bookmarks removed on X")
    bm_search = bm_sub.add_parser("search", parents=[common], help="full-text search of the local index",
                                  description="Full-text search of the local bookmark index, without calling X. "
                                  "Supports phrases, OR and prefix*. " + FIRST_SEEN)
    bm_search.add_argument("query")
    bm_search.add_argument("--from", dest="from_user", metavar="@USER", help="only bookmarks by this user")
    bm_search.add_argument("--since", help="duration such as 3d or 12h, or an ISO date (post creation time)")
    bm_search.add_argument("--until", help="ISO date (post creation time)")
    bm_search.add_argument("--folder", metavar="ID", help="only bookmarks in this folder")
    bm_search.add_argument("--limit", type=int, metavar="N", help="bookmarks to return, 1-500 (default 25)")
    bm_search.add_argument("--include-removed", action="store_true", help="also search bookmarks removed on X")
    bm_sub.add_parser("folders", parents=[common, spending], help="fetch and store the bookmark folder list")

    usage = sub.add_parser("usage", parents=[common], help="local spend ledger beside X's usage figures")
    usage.add_argument("--days", type=int, default=7, metavar="N", help="UTC days to show, 1-90 (default 7)")
    return parser


def _exit_code(code):
    if code is None:
        return EXIT_OK
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return EXIT_USAGE


# Login


class _LoopbackServer(http.server.HTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind looks up the host's FQDN, which is not needed on loopback.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        # The default prints a traceback that could include the request line, which holds the code.
        pass


def _handler(path, state, results, port):
    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = 5

        def _reply(self, status, text):
            body = (text + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                parts = urlsplit(self.path)
                params = parse_qs(parts.query)
            except ValueError:
                self._reply(400, "bad request")
                return
            if parts.path != path:
                self._reply(404, "not found")
                return
            got = params.get("state", [""])[0]
            if not hmac.compare_digest(got.encode("utf-8"), state.encode("utf-8")):
                self._reply(400, "this is not the login fav is waiting for")
                return
            self._reply(200, "you can close this tab")
            results.put("http://" + LOOPBACK_HOST + ":" + str(port) + self.path)

        def log_message(self, format, *args):
            # The query string holds the authorization code; log nothing.
            pass

    return Handler


def _start_loopback(redirect_uri, state, results, stderr):
    """A started loopback server and its thread, or (None, None) after telling the operator why not."""
    try:
        parts = urlsplit(redirect_uri)
        host = parts.hostname
        port = parts.port
    except ValueError:
        host = port = None
    if host != LOOPBACK_HOST or port is None:
        stderr.write("loopback unavailable: redirect_uri is not on " + LOOPBACK_HOST + "; paste the redirect URL\n")
        return None, None
    try:
        server = _LoopbackServer((LOOPBACK_HOST, port), _handler(parts.path or "/", state, results, port))
    except OSError:
        stderr.write("loopback unavailable on " + str(port) + "; paste the redirect URL\n")
        return None, None
    try:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
    except BaseException:
        server.server_close()
        raise
    return server, thread


def _read_line(stdin, results):
    try:
        line = stdin.readline()
    except (OSError, ValueError):
        return
    if line and line.strip():
        results.put(line.strip())


def _login(ctx, *, prompt, stdin, stderr, loopback, timeout):
    verifier, challenge = auth_mod.make_pkce()
    state = auth_mod.new_state()
    prompt.write("open this URL to authorize:\n" + auth_mod.authorize_url(ctx.config, state, challenge) + "\n")
    results = queue.Queue()
    server = None
    try:
        if loopback:
            server, _thread = _start_loopback(ctx.config.redirect_uri, state, results, stderr)
        prompt.write("then paste the redirect URL here:\n")
        prompt.flush()
        threading.Thread(target=_read_line, args=(stdin, results), daemon=True).start()
        try:
            redirect = results.get(timeout=timeout)
        except queue.Empty:
            raise XError("X_AUTH_REQUIRED", "no redirect URL arrived within " + format(timeout, "g")
                         + " seconds; run fav auth login again") from None
    finally:
        if server is not None:
            # From the main thread: shutdown() inside a handler would wait on itself.
            server.shutdown()
            server.server_close()
    code = auth_mod.parse_redirect(redirect, state)
    token = ctx.auth.exchange_code(code, verifier)
    data = _me(ctx, "auth login", token["access_token"])
    ctx.auth.save_login(token, data)
    return {"username": data["username"], "user_id": data["id"]}, "logged in as @" + data["username"] + "\n"


def _me(ctx, command, bearer):
    data = ctx.client.get_exempt(command, "/2/users/me", {"user.fields": "username"}, bearer=bearer).get("data")
    if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not isinstance(data.get("username"), str):
        raise XError("X_UPSTREAM", "X returned an invalid account for /2/users/me")
    return data


def _status(ctx):
    data = _me(ctx, "auth status", ctx.auth.access_token())
    token = ctx.auth.store.load()
    if token is None:
        raise XError("X_AUTH_REQUIRED", "not signed in; " + auth_mod.LOGIN_HINT)
    auth_mod.check_scopes(token.get("scope"))
    try:
        app_token = ctx.auth.load_app_bearer() is not None
        app_text = "present" if app_token else "missing"
    except XError:
        app_token, app_text = False, "invalid; run fav auth app-token"
    result = {"username": data["username"], "user_id": data["id"], "scope": token.get("scope"),
              "expires_at": token["expires_at"], "app_token": app_token}
    plain = ("@" + data["username"] + " (id " + data["id"] + ")\n"
             "scopes: " + str(token.get("scope")) + "\n"
             "expires " + token["expires_at"] + "\n"
             "app token: " + app_text + "\n")
    return result, plain


def _app_token(ctx, getpass_fn):
    try:
        value = getpass_fn("X app bearer token: ")
    except EOFError:
        raise XError("X_BAD_INPUT", "no app bearer token entered") from None
    ctx.auth.save_app_bearer(value)
    return {"app_token": True}, "app token saved\n"


def _logout(ctx):
    revoked = ctx.auth.logout()
    plain = "logged out\n" if revoked else (
        "logged out (nothing was revoked at X; if you were signed in, revoke the app in X settings)\n")
    return {"logged_out": True, "revoked": revoked}, plain


def _bookmarks(ctx, args):
    if args.bookmarks_command == "sync":
        return bookmarks_mod.sync(ctx, full=args.full)
    if args.bookmarks_command == "folders":
        return bookmarks_mod.list_folders(ctx)
    if args.bookmarks_command == "search":
        return commands.bookmarks_search(ctx, args.query, from_user=args.from_user, since=args.since,
                                         until=args.until, folder=args.folder, limit=args.limit,
                                         include_removed=args.include_removed)
    return commands.bookmarks_list(ctx, folder=args.folder, limit=args.limit, offset=args.offset,
                                   include_removed=args.include_removed)


def _dispatch(ctx, args, io):
    if args.command == "auth":
        prompt = io["stderr"] if args.json else io["stdout"]
        if args.auth_command == "login":
            return _login(ctx, prompt=prompt, stdin=io["stdin"], stderr=io["stderr"], loopback=io["loopback"],
                          timeout=io["login_timeout"])
        if args.auth_command == "status":
            return _status(ctx)
        if args.auth_command == "app-token":
            return _app_token(ctx, io["getpass_fn"])
        return _logout(ctx)
    if args.command == "search":
        return commands.search(ctx, args.query, since=args.since, until=args.until, from_user=args.from_user,
                               lang=args.lang, limit=args.limit, next_token=args.next_token), None
    if args.command == "post":
        return commands.post(ctx, args.ref), None
    if args.command == "thread":
        return commands.thread(ctx, args.ref, limit=args.limit), None
    if args.command == "user":
        return commands.user(ctx, args.ref), None
    if args.command == "bookmarks":
        return _bookmarks(ctx, args), None
    return commands.usage(ctx, args.days), None


def main(argv: list[str], *, env: Mapping[str, str] | None = None, stdout=None, stderr=None, stdin=None,
         transport=None, now_fn=None, sleep=time.sleep, getpass_fn=None, loopback: bool = True,
         login_timeout: float = 600.0) -> int:
    env = os.environ if env is None else env
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    stdin = sys.stdin if stdin is None else stdin
    now_fn = (lambda: datetime.now(timezone.utc)) if now_fn is None else now_fn
    getpass_fn = getpass.getpass if getpass_fn is None else getpass_fn

    parser = _parser()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            args = parser.parse_args(argv)
        except SystemExit as e:
            return _exit_code(e.code)

    as_json = getattr(args, "json", False)
    io = {"stdout": stdout, "stderr": stderr, "stdin": stdin, "getpass_fn": getpass_fn, "loopback": loopback,
          "login_timeout": login_timeout}
    try:
        config_mod.check_environment()
        ctx = commands.build_context(env, transport=UrllibTransport() if transport is None else transport,
                                     now_fn=now_fn, sleep=sleep, extra_usd=getattr(args, "budget", None) or 0.0)
        try:
            result, plain = _dispatch(ctx, args, io)
        finally:
            ctx.store.close()
    except XError as e:
        if as_json:
            stdout.write(output.render_json(e.to_json()))
        else:
            stderr.write(output.terminal_safe(str(e)) + "\n")
        return EXIT_ERROR
    if as_json:
        stdout.write(output.render_json(result))
    else:
        stdout.write(output.terminal_safe(plain) if plain is not None else output.render_plain(result, now_fn()))
    return EXIT_OK
