import importlib.metadata
import sys

import click
import sbt.commands.list
from loguru import logger
from sbt.commands.build import build
from sbt.commands.git import git
from sbt.commands.install import add, install
from sbt.registry.pypi import PyPI

try:
    version = importlib.metadata.version("pbt-simple")
except importlib.metadata.PackageNotFoundError:
    version = "0.0.0"


def check_upgrade():
    latest_version = PyPI.get_instance().get_latest_version("pbt-simple")
    if latest_version is not None and version != latest_version:
        logger.warning(
            f"You are using an outdated version of sbt (pbt-simple). The latest version is {latest_version}, while you are using {version}."
        )
    else:
        logger.trace("You are using the latest version of sbt (pbt-simple).")


@click.group(
    help=f"SBT ({version}) -- a simple python build tool for multi-projects that supports poetry and maturin (thus support extension module written in PYO3 (Rust))"
)
@click.version_option(version)
def cli():
    check_upgrade()


cli.add_command(install)
cli.add_command(add)
cli.add_command(build)
cli.add_command(git)
cli.add_command(sbt.commands.list.list)

if __name__ == "__main__":
    cli()
