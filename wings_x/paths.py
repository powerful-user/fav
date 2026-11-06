import os
import tempfile
from dataclasses import dataclass
from typing import Mapping

from wings_x.errors import XError, local_io_error


@dataclass(frozen=True)
class Paths:
    config_dir: str
    state_dir: str
    releases_dir: str

    @property
    def config_file(self):
        return os.path.join(self.config_dir, "config.toml")

    @property
    def token_file(self):
        return os.path.join(self.config_dir, "token.json")

    @property
    def app_bearer_file(self):
        return os.path.join(self.config_dir, "app-bearer")

    @property
    def token_lock(self):
        return os.path.join(self.config_dir, "token.lock")

    @property
    def db_file(self):
        return os.path.join(self.state_dir, "x.sqlite3")

    @property
    def sync_lock(self):
        return os.path.join(self.state_dir, "sync.lock")


def _xdg_or_home(env, key, *home_parts):
    value = env.get(key)
    if value:
        return value
    home = env.get("HOME")
    if not home:
        raise XError("X_CONFIG", "HOME is not set")
    return os.path.join(home, *home_parts)


def resolve_paths(env: Mapping[str, str]) -> Paths:
    config_root = _xdg_or_home(env, "XDG_CONFIG_HOME", ".config")
    state_root = _xdg_or_home(env, "XDG_STATE_HOME", ".local", "state")
    data_root = _xdg_or_home(env, "XDG_DATA_HOME", ".local", "share")
    return Paths(
        config_dir=os.path.join(config_root, "wings", "x"),
        state_dir=os.path.join(state_root, "wings", "x"),
        releases_dir=os.path.join(data_root, "wings-x", "releases"),
    )


def ensure_dirs(paths: Paths) -> None:
    for directory in (paths.config_dir, paths.state_dir):
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        except OSError as exc:
            raise local_io_error("create", directory, exc) from None


def _write_all(fd, data):
    view = memoryview(data)
    while len(view) > 0:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("write made no progress")
        view = view[written:]


def atomic_write(path: str, data: bytes) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory)
    replaced = False
    try:
        try:
            os.chmod(tmp_path, 0o600)
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
        replaced = True
        os.chmod(path, 0o600)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        if not replaced:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise
