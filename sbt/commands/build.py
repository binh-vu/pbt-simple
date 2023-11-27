from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path
from uuid import uuid4

import click
from loguru import logger

from sbt.commands.install import PASSTHROUGH_ENVS
from sbt.config import PBTConfig
from sbt.misc import exec
from sbt.package.discovery import discover_packages


@click.command()
@click.argument("package", default="")
@click.option("--cwd", default=".", help="Override current working directory")
@click.option(
    "-r",
    "--release",
    is_flag=True,
    help="release mode",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="increase verbosity",
)
def build(package: str, cwd: str = ".", release: bool = False, verbose: bool = False):
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

    if package == "":
        # use the package located in the current directory
        for pkg in packages.values():
            if pkg.location == cfg.cwd:
                package = pkg.name
                break
        else:
            raise ValueError(
                f"Cannot find a package in the current directory {cfg.cwd}"
            )

    if package not in packages:
        logger.error(
            f"Cannot find package {package} perhaps the project configuration is incorrect."
        )
        return

    pkg = packages[package]
    outdir = pkg.location / "dist" / str(uuid4())
    outdir.mkdir(parents=True, exist_ok=True)

    cmd = ["maturin", "build"]
    if release:
        cmd.append("-r")
    cmd.extend(["-o", str(outdir)])
    exec(
        cmd,
        cwd=str(pkg.location),
        env=PASSTHROUGH_ENVS,
    )

    (whl_file,) = [x for x in outdir.glob("*.whl")]
    with zipfile.ZipFile(whl_file, "r") as zip_ref:
        zip_ref.extractall(outdir)

    pkg_name = pkg.name.replace("-", "_")
    pkg_dir = outdir / pkg_name
    if not pkg_dir.exists():
        for name in pkg.include:
            pkg_name = name.replace("-", "_")
            pkg_dir = outdir / pkg_name
            if pkg_dir.exists():
                break
        else:
            raise RuntimeError(f"Cannot find the package directory in {outdir}")

    for file in pkg_dir.iterdir():
        if file.name.endswith(".so") or file.name.endswith(".dylib"):
            dest_file = pkg.location / pkg_name / file.name
            if dest_file.exists():
                dest_file.unlink()
            shutil.move(file, pkg.location / pkg_name)

    shutil.rmtree(outdir)
