"""The `spyhop` command line. Each module adds its commands to `app`."""

from spyhop.cli import checkpoints, evals, runs, service, steps  # noqa: F401
from spyhop.cli.common import app

__all__ = ["app"]
