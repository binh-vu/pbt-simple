from __future__ import annotations

import os
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional, cast

import click
from loguru import logger
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
    parse_version,
    parse_version_spec,
)
from sbt.package.graph import PkgGraph
from sbt.package.package import DepConstraint, DepConstraints, Package
from tomlkit.api import document, dumps, inline_table, loads, nl, table
from tomlkit.items import Array, KeyType, SingleKey, Trivia
from tqdm.auto import tqdm

# environment variables that will be passed to the subprocess
PASSTHROUGH_ENVS = [
    "PATH",
    "CC",
    "CXX",
    "LIBCLANG_PATH",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH",
    "HOME",
    "CARGO_HOME",
    "RUSTUP_HOME",
]


@click.command()
@click.option("--cwd", default=".", help="Override current working directory")
@click.option(
    "--ignore-invalid-pkg",
    is_flag=True,
    help="whether to ignore invalid packages",
)
def list(
    cwd: str = ".",
    ignore_invalid_pkg: bool = False,
):
    cwd = os.path.abspath(cwd)
    cfg = PBTConfig.from_dir(cwd)

    # discovery packages
    packages = discover_packages(
        cfg.cwd,
        cfg.cache_dir,
        cfg.ignore_directories,
        cfg.ignore_directory_names,
        ignore_invalid_package=ignore_invalid_pkg,
    )

    print("Found the following packages:")
    for package in packages.values():
        print("\t-", package.name)
