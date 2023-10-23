from __future__ import annotations

import os
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional, cast

import click
from loguru import logger
from tomlkit.api import document, dumps, inline_table, loads, nl, table
from tomlkit.items import Array, KeyType, SingleKey, Trivia
from tqdm.auto import tqdm

from sbt.config import PBTConfig
from sbt.misc import (
    ExecProcessError,
    IncompatibleDependencyError,
    NewEnvVar,
    exec,
    mask_file,
    venv_path,
)
from sbt.package.discovery import discover_packages, parse_version, parse_version_spec
from sbt.package.package import DepConstraint, DepConstraints, Package

# environment variables that will be passed to the subprocess
PASSTHROUGH_ENVS = [
    "PATH",
    "CC",
    "CXX",
    "LIBCLANG_PATH",
    "LD_LIBRARY_PATH",
    "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH",
    "CARGO_HOME",
    "RUSTUP_HOME",
]


@click.command()
@click.argument("package", default="")
@click.option("--cwd", default=".", help="Override current working directory")
@click.option(
    "--ignore-invalid-pkg",
    is_flag=True,
    help="whether to ignore invalid packages",
)
@click.option(
    "--ignore-invalid-dependency",
    is_flag=True,
    help="whether to ignore invalid dependencies",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="increase verbosity",
)
def install(
    package: str,
    cwd: str = ".",
    ignore_invalid_pkg: bool = False,
    ignore_invalid_dependency: bool = False,
    verbose: bool = False,
):
    cwd = os.path.abspath(cwd)
    cfg = PBTConfig.from_dir(cwd)

    # discovery packages
    packages = discover_packages(
        cfg.cwd,
        cfg.ignore_directories,
        cfg.ignore_directory_names,
        ignore_invalid_package=ignore_invalid_pkg,
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

    # now install the packages
    # step 1: gather all dependencies in one file and install it.
    thirdparty_pkgs: dict[str, tuple[set[str], DepConstraints]] = {}
    invalid_thirdparty_pkgs: set[str] = set()
    for pkg in packages.values():
        for depname, depspecs in pkg.dependencies.items():
            if depname in packages or depname in invalid_thirdparty_pkgs:
                continue
            if depname not in thirdparty_pkgs:
                thirdparty_pkgs[depname] = ({pkg.name}, depspecs)
                continue

            try:
                thirdparty_pkgs[depname][0].add(pkg.name)
                thirdparty_pkgs[depname] = (
                    thirdparty_pkgs[depname][0],
                    find_common_specs(thirdparty_pkgs[depname][1], depspecs),
                )
            except IncompatibleDependencyError:
                logger.error(
                    "Encounter an incompatible dependency {}. Found it in:\n{}",
                    depname,
                    "\n".join(
                        f"\t- {packages[pkgname].location}"
                        for pkgname in thirdparty_pkgs[depname][0]
                    ),
                )

                if ignore_invalid_dependency:
                    invalid_thirdparty_pkgs.add(depname)
                else:
                    raise

    # step 2: install local packages in editable mode
    install_pkg_dependencies(
        packages[package],
        {depname: depspecs for depname, (_, depspecs) in thirdparty_pkgs.items()},
        cfg,
    )

    local_dep_pkgs = [pkg for pkg in packages.values() if pkg.name != package]
    logger.info(
        "Installing local packages: {}", ", ".join(pkg.name for pkg in local_dep_pkgs)
    )
    pkg_venv_path = venv_path(
        packages[package].name,
        packages[package].location,
        cfg.python_virtualenvs_path,
        cfg.get_python_path(),
    )
    for pkg in tqdm(local_dep_pkgs):
        install_bare_pkg(pkg, cfg, pkg_venv_path)

    # step 3: check if we need to build and install any extension module


def install_bare_pkg(pkg: Package, cfg: PBTConfig, virtualenv: Optional[Path] = None):
    """Install a package without any dependencies in editable mode"""
    with mask_file(pkg.location / "pyproject.toml"), mask_file(
        pkg.location / "poetry.lock"
    ):
        with open(pkg.location / "pyproject.toml", "w") as f:
            doc = document()

            tbl = table()
            tbl.add("name", pkg.name)
            tbl.add("version", pkg.version)
            tbl.add("description", "")
            tbl.add("authors", [])
            if sum(int(x != pkg.name) for x in pkg.include) > 0:
                tbl.add("packages", [{"include": x} for x in pkg.include])

            doc.add(SingleKey("tool.poetry", t=KeyType.Bare), tbl)

            tbl = table()
            tbl.add("requires", ["poetry-core>=1.0.0"])
            tbl.add("build-backend", "poetry.core.masonry.api")
            doc.add(nl())
            doc.add("build-system", tbl)

            f.write(dumps(doc))

        install_poetry_package(pkg, cfg, virtualenv, quiet=True)


def install_pkg_dependencies(
    pkg: Package,
    deps: dict[str, DepConstraints],
    cfg: PBTConfig,
    virtualenv: Optional[Path] = None,
):
    with open(cfg.pkg_cache_dir(pkg) / "pyproject.modified.toml", "w") as f:
        doc = document()

        tbl = table()
        tbl.add("name", pkg.name)
        tbl.add("version", pkg.version)
        tbl.add("description", "")
        tbl.add("authors", [])
        if sum(int(x != pkg.name) for x in pkg.include) > 0:
            tbl.add("packages", [{"include": x} for x in pkg.include])

        doc.add(SingleKey("tool.poetry", t=KeyType.Bare), tbl)

        tbl = table()
        for dep, specs in deps.items():
            tbl.add(dep, serialize_dep_specs(specs))
        doc.add(nl())
        doc.add(SingleKey("tool.poetry.dependencies", t=KeyType.Bare), tbl)

        tbl = table()
        tbl.add("requires", ["poetry-core>=1.0.0"])
        tbl.add("build-backend", "poetry.core.masonry.api")
        doc.add(nl())
        doc.add("build-system", tbl)

        f.write(dumps(doc))

    try:
        os.rename(
            pkg.location / "pyproject.toml",
            cfg.pkg_cache_dir(pkg) / "pyproject.origin.toml",
        )
        if (pkg.location / "poetry.lock").exists():
            os.rename(
                pkg.location / "poetry.lock",
                cfg.pkg_cache_dir(pkg) / "poetry.origin.lock",
            )
        shutil.copy(
            cfg.pkg_cache_dir(pkg) / "pyproject.modified.toml",
            pkg.location / "pyproject.toml",
        )
        if (cfg.pkg_cache_dir(pkg) / "poetry.modified.lock").exists():
            shutil.copy(
                cfg.pkg_cache_dir(pkg) / "poetry.modified.lock",
                pkg.location / "poetry.lock",
            )

        install_poetry_package(pkg, cfg, virtualenv)
    finally:
        os.rename(
            cfg.pkg_cache_dir(pkg) / "pyproject.origin.toml",
            pkg.location / "pyproject.toml",
        )
        if (pkg.location / "poetry.lock").exists():
            os.rename(
                pkg.location / "poetry.lock",
                cfg.pkg_cache_dir(pkg) / "poetry.modified.lock",
            )
        if (cfg.pkg_cache_dir(pkg) / "poetry.origin.lock").exists():
            os.rename(
                cfg.pkg_cache_dir(pkg) / "poetry.origin.lock",
                pkg.location / "poetry.lock",
            )


def install_poetry_package(
    pkg: Package,
    cfg: PBTConfig,
    virtualenv: Optional[Path] = None,
    quiet: bool = False,
):
    if virtualenv is None:
        virtualenv = venv_path(
            pkg.name,
            pkg.location,
            cfg.python_virtualenvs_path,
            cfg.get_python_path(),
        )

    env: list[str | NewEnvVar] = [x for x in PASSTHROUGH_ENVS if x != "PATH"]
    for k, v in get_virtualenv_environment_variables(virtualenv).items():
        env.append({"name": k, "value": v})

    if (pkg.location / "poetry.lock").exists():
        try:
            exec(
                "poetry check --lock" + (" -q" if quiet else ""),
                cwd=pkg.location,
                env=env,
            )
        except ExecProcessError:
            logger.debug(
                "Package {} poetry.lock is inconsistent with pyproject.toml, updating lock file...",
                pkg.name,
            )
            exec(
                "poetry lock --no-update" + (" -q" if quiet else ""),
                cwd=pkg.location,
                capture_stdout=False,
                env=env,
            )

    exec(
        f"poetry install" + (" -q" if quiet else ""),
        cwd=pkg.location,
        capture_stdout=False,
        env=env,
    )


def get_virtualenv_environment_variables(virtualenv: Path) -> dict:
    return {
        "VIRTUAL_ENV": str(virtualenv),
        "PATH": str(virtualenv / "bin") + os.pathsep + os.environ.get("PATH", ""),
    }


def find_common_specs(
    depspecs: DepConstraints, another_depspecs: DepConstraints
) -> DepConstraints:
    """Find the common specs between two dependencies."""
    specs = {x.constraint or "": x for x in depspecs}
    anotherspecs = {x.constraint or "": x for x in another_depspecs}

    if not (len(specs) == len(depspecs) and len(anotherspecs) == len(another_depspecs)):
        raise IncompatibleDependencyError(
            f"Two dependencies have duplicated specs: {depspecs} and {another_depspecs}"
        )

    if specs.keys() != anotherspecs.keys():
        raise IncompatibleDependencyError(
            f"Two dependencies have different number of specs: {depspecs} and {another_depspecs}"
        )

    newspecs = []
    for constraint, spec in specs.items():
        anotherspec = anotherspecs[constraint]

        specver = parse_version_spec(spec.version_spec)
        anotherspecver = parse_version_spec(anotherspec.version_spec)

        # we should not have ValueError exception because we have checked the compatibility
        try:
            specver = specver.intersect(anotherspecver)
        except ValueError:
            raise IncompatibleDependencyError(
                f"Two dependencies have incompatible specs: {depspecs} and {another_depspecs}"
            )

        newspecs.append(
            DepConstraint(
                specver.to_pep508_string(),
                constraint,
                spec.version_spec_field,
                spec.origin_spec,
            )
        )
    return newspecs


def serialize_dep_specs(specs: DepConstraints) -> Array:
    items = []
    for spec in specs:
        if spec.origin_spec is None:
            item = spec.version_spec
        else:
            item = inline_table()
            item[cast(str, spec.version_spec_field)] = spec.version_spec
            for k, v in spec.origin_spec.items():
                item[k] = v
        items.append(item)

    if len(items) == 1:
        return items[0]
    return Array(items, Trivia(), multiline=True)
