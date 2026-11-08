CODES = (
    "X_CONFIG",
    "X_ENVIRONMENT",
    "X_AUTH_REQUIRED",
    "X_AUTH_EXPIRED",
    "X_SCOPE_MISMATCH",
    "X_API_ACCESS_REQUIRED",
    "X_CREDITS_EXHAUSTED",
    "X_BUDGET_EXCEEDED",
    "X_RATE_LIMITED",
    "X_NOT_FOUND",
    "X_PROTECTED",
    "X_BAD_INPUT",
    "X_QUERY_INVALID",
    "X_UPSTREAM",
    "X_LOCAL_IO",
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


class XError(Exception):
    def __init__(self, code, message, retry_at=None):
        if code not in CODES:
            raise ValueError(code)
        self.code = code
        self.message = message
        self.retry_at = retry_at
        super().__init__(str(self))

    def to_json(self):
        error = {"code": self.code, "message": self.message}
        if self.retry_at is not None:
            error["retry_at"] = self.retry_at
        return {"error": error}

    def __str__(self):
        return self.code + ": " + self.message


def local_io_error(action, path, exc):
    """X_LOCAL_IO for a local file failure (Global Constraints decision 20).

    The reason is the system's: strerror, else SQLite's error name, else the type name.
    An sqlite3 message can quote the database's schema, so it is never used.
    """
    reason = getattr(exc, "strerror", None) or getattr(exc, "sqlite_errorname", None) or type(exc).__name__
    return XError("X_LOCAL_IO", "cannot " + action + " " + path + ": " + reason)
