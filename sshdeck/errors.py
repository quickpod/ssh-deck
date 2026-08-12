"""Error types for sshdeck."""


class SSHDeckError(Exception):
    """Raised for any recoverable failure in an sshdeck operation.

    Every public function raises this (and only this) on an expected failure --
    a bad session, an auth rejection, an unreachable host, a malformed forward
    spec -- so callers (the CLI and the GUI) have a single exception to catch and
    can surface a clean message instead of a paramiko/socket traceback.
    """
