"""Errors that can be reported by the CLI without a traceback."""


class KeeperError(Exception):
    """An actionable authentication or backup failure."""
