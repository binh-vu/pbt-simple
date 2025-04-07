from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional, cast

import click
from loguru import logger
from rich import print
from sbt.config import PBTConfig
from sbt.misc import (
    ExecProcessError,
    IncompatibleDependencyError,
    NewEnvVar,
    exec,
    mask_file,
    venv_path,
)
from sbt.package.discovery import (
    discover_packages,
    parse_pep518_pyproject,
    parse_version_spec,
)
from sbt.package.graph import PkgGraph
from sbt.package.package import DepConstraint, DepConstraints, Package
from sbt.registry.pypi import PyPI
from tomlkit.api import document, dumps, inline_table, nl, table
from tomlkit.items import Array, KeyType, SingleKey, Trivia
from tqdm.auto import tqdm


@click.command()
@click.option("--cwd", default=".", help="Override current working directory")
def check_release(cwd: str = "."):
    cwd = os.path.abspath(cwd)
    cfg = PBTConfig.from_dir(cwd)

    # discovery packages
    packages = discover_packages(
        cfg.cwd,
        cfg.cache_dir,
        cfg.ignore_directories,
        cfg.ignore_directory_names,
        ignore_invalid_package=True,
    )

    pypi = PyPI.get_instance()

    print("Checking release version of {} packages".format(len(packages)))
    for index, pkg in enumerate(packages.values()):
        latest_version = pypi.get_latest_version(pkg.name)
        if latest_version != pkg.version:
            print(
                f"[bold yellow]({index}/{len(packages)}) Package {pkg.name} in PyPI is outdated (version {pkg.version}, pypi version {latest_version})[/bold yellow]"
            )
        else:
            print(
                f"({index}/{len(packages)}) Package {pkg.name} in PyPI is up to date (version {pkg.version})"
            )
