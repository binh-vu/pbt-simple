from __future__ import annotations

import os

import click

from sbt.config import PBTConfig
from sbt.package.discovery import discover_packages

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
@click.option("-v", "--verbose", count=True)
def list(
    cwd: str = ".",
    ignore_invalid_pkg: bool = False,
    verbose: int = 0,
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
        verbose=verbose,
    )

    print("Found the following packages:")
    for package in packages.values():
        print("\t-", package.name)
