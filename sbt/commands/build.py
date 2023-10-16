from __future__ import annotations

from pathlib import Path

import click

from sbt.config import PBTConfig


@click.command()
@click.argument("package")
@click.option("--cwd", default=".", help="Override current working directory")
def build(package: str, cwd: str = "."):
    cfg = PBTConfig.from_dir(cwd)
