"""Error codes, exit statuses and the refusal contract.

Exit status: 0 ok, 1 refusal (ERR[code) on stderr), 2 usage (argparse), 130 interrupt.
Every invocation still prints exactly one JSON object on stdout.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_INTERRUPT = 130


class MvError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.detail = detail

    def to_json(self) -> dict:
        err: dict = {"code": self.code, "message": self.message}
        if self.hint:
            err["hint"] = self.hint
        if self.detail is not None:
            err["detail"] = self.detail
        return err


class DryRun(Exception):
    """Raised by a mutating verb under --dry-run; carries the plan."""

    def __init__(self, plan: list):
        super().__init__("dry run")
        self.plan = plan
