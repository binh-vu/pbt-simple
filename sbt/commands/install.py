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
    """Install a package and its local dependencies in editable mode"""
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
    loc2pkg = {pkg.location: pkg for pkg in packages.values()}
    if package == "":
        # use the package located in the current directory
        if cfg.cwd in loc2pkg:
            package = loc2pkg[cfg.cwd].name
        else:
            raise ValueError(
                f"Cannot find a package in the current directory {cfg.cwd}"
            )

    install_pkg(packages[package], packages, cfg, ignore_invalid_dependency, None)


@click.command()
@click.argument("dependency")
@click.option("--package", default="", help="The target package to add dependency to")
@click.option("--cwd", default=".", help="Override current working directory")
@click.option(
    "--no-dep-dep", is_flag=True, help="Do not install dependencies of the dependency"
)
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
def add(
    dependency: str,
    package: str = "",
    cwd: str = ".",
    no_dep_dep: bool = False,
    ignore_invalid_pkg: bool = False,
    ignore_invalid_dependency: bool = False,
    verbose: bool = False,
):
    """Add a local package as a dependency to the target package"""
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
    loc2pkg = {pkg.location: pkg for pkg in packages.values()}
    if package == "":
        # use the package located in the current directory
        if cfg.cwd in loc2pkg:
            package = loc2pkg[cfg.cwd].name
        else:
            raise ValueError(
                f"Cannot find a package in the current directory {cfg.cwd}"
            )

    # step 1: get the dependency package and add its to the list of discover packages
    if dependency not in packages:
        # dependency is a path
        deppkg = parse_pep518_pyproject(Path(dependency))
        packages[deppkg.name] = deppkg
    else:
        deppkg = packages[dependency]

    # step 2: update the list of manually installed
    pkg_cache_dir = cfg.pkg_cache_dir(packages[package])
    manual_deps = packages[package].find_manually_installed_dependencies(pkg_cache_dir)
    manual_deps.append(deppkg.location)
    packages[package].save_manually_installed_dependencies(
        pkg_cache_dir, sorted(set(manual_deps))
    )

    # step 3: gather dependencies of the package -- include the one from manually installed packages
    if no_dep_dep:
        pkg_venv_path = venv_path(
            packages[package].name,
            packages[package].location,
            cfg.python_virtualenvs_path,
            cfg.get_python_path(),
        )
        install_bare_pkg(packages[package], cfg, pkg_venv_path)
    else:
        install_pkg(
            packages[package], packages, cfg, ignore_invalid_dependency, [deppkg]
        )


def install_pkg(
    target_pkg: Package,
    packages: dict[str, Package],
    cfg: PBTConfig,
    ignore_invalid_dependency: bool,
    local_dep_pkgs: Optional[list[Package]],
):
    loc2pkg = {pkg.location: pkg for pkg in packages.values()}

    # step 0: gather dependencies of the package -- include the one from manually installed packages
    target_pkg.dependencies.update(
        {
            (p := loc2pkg[loc]).name: [
                DepConstraint(version_spec="=={}".format(p.version))
            ]
            for loc in target_pkg.find_manually_installed_dependencies(
                cfg.pkg_cache_dir(target_pkg)
            )
        }
    )
    pkg_graph = PkgGraph.from_pkgs(packages)

    thirdparty_pkgs: dict[str, tuple[set[str], DepConstraints]] = {}
    invalid_thirdparty_pkgs: set[str] = set()

    for thirdparty_pkg in pkg_graph.third_party_dependencies(target_pkg.name):
        assert thirdparty_pkg.name not in thirdparty_pkgs
        depspecs = None
        for p, pc in thirdparty_pkg.invert_dependencies.items():
            if depspecs is None:
                depspecs = pc
            else:
                try:
                    depspecs = find_common_specs(depspecs, pc)
                except IncompatibleDependencyError:
                    logger.error(
                        "Encounter an incompatible dependency {}. Found it in:\n{}",
                        thirdparty_pkg.name,
                        "\n".join(
                            f"\t- {packages[pkgname].location}"
                            for pkgname in thirdparty_pkg.invert_dependencies.keys()
                        ),
                    )

                    if ignore_invalid_dependency:
                        invalid_thirdparty_pkgs.add(thirdparty_pkg.name)
                    else:
                        raise
        assert depspecs is not None
        thirdparty_pkgs[thirdparty_pkg.name] = (
            set(thirdparty_pkg.invert_dependencies.keys()),
            depspecs,
        )

    # now install the target package
    # step 1: gather all dependencies in one file and install it.
    install_pkg_dependencies(
        target_pkg,
        {depname: depspecs for depname, (_, depspecs) in thirdparty_pkgs.items()},
        cfg,
    )

    # step 2: install all local packages in editable mode
    if local_dep_pkgs is None:
        local_dep_pkgs = [pkg for pkg in pkg_graph.local_dependencies(target_pkg.name)]

    pkg_venv_path = venv_path(
        target_pkg.name,
        target_pkg.location,
        cfg.python_virtualenvs_path,
        cfg.get_python_path(),
    )

    if len(local_dep_pkgs) > 0:
        logger.info(
            "Installing local packages: {}",
            ", ".join(pkg.name for pkg in local_dep_pkgs),
        )
        for pkg in tqdm(local_dep_pkgs):
            install_bare_pkg(pkg, cfg, pkg_venv_path)

    # step 3: check if we need to build and install any extension module
    # TODO: implement this -- for now, users can use the `build` command to build manually
    logger.info(
        "Finished installing package {} to env {}", target_pkg.name, pkg_venv_path
    )


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
        if "python" not in deps:
            tbl.add("python", f"=={cfg.get_python_version()}")
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


# def load_external_dependencies(pkg: Package, cfg: PBTConfig, packages: dict[str, Package]) -> dict[str, Package]:
