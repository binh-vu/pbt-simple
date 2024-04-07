from __future__ import annotations

import os
import re
from operator import attrgetter
from pathlib import Path
from typing import cast

import semver
from loguru import logger
from sbt.misc import InvalidPackageError
from sbt.package.package import (
    DepConstraint,
    DepConstraints,
    Package,
    PackageType,
    VersionSpec,
)
from tomlkit.api import loads


def discover_packages(
    root: Path,
    cache_dir: Path,
    ignore_dirs: set[Path],
    ignore_dirnames: set[str],
    ignore_invalid_package: bool = False,
):
    """Find all packages in the given directory and manually linked/installed packages."""
    candidate_pyprojects = get_candidate_pyprojects(root, ignore_dirs, ignore_dirnames)

    # mapping from package directory to its configuration
    pkgs: dict[str, Package] = {}

    for loc in candidate_pyprojects:
        try:
            pkg = parse_pep518_pyproject(loc)
        except InvalidPackageError as e:
            if not ignore_invalid_package:
                raise e
            logger.warning(f"An package at {loc} is invalid. Ignore it. Error: {e}")
            continue
        except Exception as e:
            logger.error(f"An error occurred when parsing {loc}")
            raise e

        if pkg.name in pkgs:
            raise RuntimeError(
                f"Duplicate package {pkg.name}: found in {pkg.location} and {pkgs[pkg.name].location}"
            )
        pkgs[pkg.name] = pkg

    # now find the manually linked/installed packages
    for pkg in list(pkgs.values()):
        pkg_cache_dir = cache_dir / pkg.name
        pkg_cache_dir.mkdir(exist_ok=True, parents=True)

        for loc in pkg.find_manually_installed_dependencies(pkg_cache_dir):
            try:
                pkg = parse_pep518_pyproject(loc)
            except InvalidPackageError as e:
                if not ignore_invalid_package:
                    raise e
                logger.warning(f"An package at {loc} is invalid. Ignore it. Error: {e}")
                continue

            if pkg.name in pkgs:
                raise RuntimeError(
                    f"Duplicate package {pkg.name}: found in {pkg.location} and {pkgs[pkg.name].location}"
                )
            pkgs[pkg.name] = pkg

    return pkgs


def get_candidate_pyprojects(
    root: Path, ignore_dirs: set[Path], ignore_dirnames: set[str]
) -> list[Path]:
    outs = {}
    root = root.resolve()

    stack = [root]
    while len(stack) > 0:
        dir = stack.pop()
        if (
            dir.name.startswith(".")
            or dir.name in ignore_dirnames
            or dir in ignore_dirs
        ):
            continue
        if (dir / "pyproject.toml").exists():
            outs[dir.absolute()] = 1
        stack.extend([subdir for subdir in dir.iterdir() if subdir.is_dir()])

    return list(outs.keys())


def parse_pep518_pyproject(loc: Path) -> Package:
    """Parse project metadata from the directory.

    Arguments:
        pyproject_file: path to the pyproject.toml file

    Returns: dict, force the type to dictionary to silence the annoying type checker due to tomlkit
    """
    try:
        with open(loc / "pyproject.toml", "r") as f:
            cfg = cast(dict, loads(f.read()))
    except:
        logger.error("Invalid TOML file: {}", loc / "pyproject.toml")
        raise

    backend = cfg["build-system"]["build-backend"]
    if backend == "maturin":
        return parse_maturin_project(cfg, loc)
    if backend == "poetry.core.masonry.api":
        return parse_poetry_project(cfg, loc)
    raise NotImplementedError(f"Unsupported build backend: {backend}")


def parse_maturin_project(cfg: dict, loc: Path) -> Package:
    name = cfg["project"]["name"]
    version = cfg["project"]["version"]

    dependencies = {}
    for item in cfg["project"]["dependencies"]:
        k, specs = parse_pep518_dep_spec(item)
        dependencies[k] = specs

    for extra, extra_deps in cfg["project"].get("optional-dependencies", {}).items():
        for item in extra_deps:
            if item.find(" ") == -1:
                # it should be a self reference dependency, so we just ignore it
                k, extras = parse_pep518_pkgname_with_extra(item)
                assert (
                    k == name
                ), f"{k} doesn't have version (because of no space -- if it does have version, add space to format it nicely), so it must be self-reference"
                continue

            k, specs = parse_pep518_dep_spec(item)
            # the dependency sometimes is self reference, so we just ignore it
            if k == name:
                continue
            dependencies[k] = specs

    # not supported yet in pep-621
    # https://peps.python.org/pep-0621/#specify-files-to-include-when-building
    include = []
    if "tool" in cfg and "maturin" in cfg["tool"]:
        if "include" in cfg["tool"]["maturin"]:
            include = cfg["tool"]["maturin"]["include"]

    return Package(name, version, loc, PackageType.Maturin, include, dependencies)


def parse_poetry_project(cfg: dict, loc: Path) -> Package:
    name = cfg["tool"]["poetry"]["name"]
    version = cfg["tool"]["poetry"]["version"]

    dependencies = {}

    for cfgkey in ["dependencies", "dev-dependencies"]:
        for k, vs in cfg["tool"]["poetry"].get(cfgkey, {}).items():
            if not isinstance(vs, list):
                vs = [vs]
            dependencies[k] = sorted(
                (parse_poetry_dep_spec(v) for v in vs),
                key=attrgetter("constraint"),
            )

    # see https://python-poetry.org/docs/pyproject/#include-and-exclude
    # and https://python-poetry.org/docs/pyproject/#packages
    include = cfg["tool"]["poetry"].get("include", [])
    for pkg_cfg in cfg["tool"]["poetry"].get("packages", []):
        include.append(os.path.join(pkg_cfg.get("from", ""), pkg_cfg["include"]))
    include = sorted(set(include))

    return Package(name, version, loc, PackageType.Poetry, include, dependencies)


def parse_pep518_pkgname_with_extra(name: str) -> tuple[str, list[str]]:
    """Parse a spec containing extra dependencies: `<name>([(<extra>,)+])?"""
    m = re.match(r"([^\[]+)(?:\[(.+)\])?$", name)
    assert m is not None, name

    # TODO: this will throw an error
    name = m.group(1)
    if m.group(2) is not None:
        extras = m.group(2).split(",")
        assert len(extras) > 0
        return name, extras
    return name, []


def parse_pep518_dep_spec(
    spec: str, allow_self_reference: bool = False
) -> tuple[str, DepConstraints]:
    """Parse a dependency specification.

    It does not support PEP508 but only a simple syntax: `<name>([(<extra>,)+])? <version_rule>`.
    Note: the space is important.
    """
    name, version = spec.split(" ", 1)
    name, extras = parse_pep518_pkgname_with_extra(name)
    if len(extras) > 0:
        origin_spec = {"extras": extras}
    else:
        origin_spec = None

    # do it here to make sure we can parse this version
    parse_version_spec(version)
    constraint = f"python=* markers="
    return name, [
        DepConstraint(
            version_spec=version, constraint=constraint, origin_spec=origin_spec
        )
    ]


def parse_poetry_dep_spec(spec: str | dict) -> DepConstraint:
    if isinstance(spec, str):
        constraint = f"python=* markers="
        return DepConstraint(version_spec=spec, constraint=constraint)
    elif isinstance(spec, dict):
        if "version" not in spec:
            if "url" in spec:
                # try to figure out the version from the URL if possible, otherwise, use 1.0.0
                m = re.search(r"\d+.\d+.\d+", spec["url"])
                if m is not None:
                    version_spec = f"=={m.group()}"
                else:
                    version_spec = "==1.0.0"
            else:
                raise NotImplementedError(
                    f"Not support specify dependency outside of Pypi yet. But found spec {spec}"
                )
        else:
            version_spec = spec["version"]

        constraint = (
            f"python={spec.get('python', '*')} markers={spec.get('markers', '')}"
        )
        origin_spec = spec.copy()
        if "version" in origin_spec:
            origin_spec.pop("version")

        return DepConstraint(
            version_spec=version_spec,
            constraint=constraint,
            version_spec_field="version",
            origin_spec=origin_spec,
        )


def parse_version_spec(
    rule: str,
) -> VersionSpec:
    """Parse the given version rule to get lowerbound and upperbound (exclusive)

    Example:
        - "^1.0.0" -> (1.0.0, 2.0.0)
        - ">= 1.0.0" -> (1.0.0, None)
        - ">= 1.0.0, < 2.1.3" -> (1.0.0, 2.1.3)
    """
    m = re.match(
        r"(?P<op1>\^|~|>|>=|==|<|<=)? *(?P<version1>[^ ,\^\~>=<]+)(?:(?:(?: *, *)|(?: +))(?P<op2>\^|~|>|>=|==|<|<=) *(?P<version2>[^ ,\^\~>=<]+))?",
        rule.strip(),
    )
    assert (
        m is not None
    ), f"The constraint is too complicated to handle for now: `{rule}`"

    op1, version1 = m.group("op1"), m.group("version1")
    op2, version2 = m.group("op2"), m.group("version2")

    if op1 == "":
        op1 = "=="

    lowerbound = parse_version(version1)
    if op1 == "^":
        assert version2 is None
        # special case for 0 following the nodejs way (I can't believe why)
        # see more: https://nodesource.com/blog/semver-tilde-and-caret/
        if lowerbound.major == 0:
            if lowerbound.minor == 0:
                upperbound = lowerbound.bump_patch()
            else:
                upperbound = lowerbound.bump_minor()
        else:
            upperbound = lowerbound.bump_major()
        spec = VersionSpec(
            lowerbound=lowerbound,
            upperbound=upperbound,
            is_lowerbound_inclusive=True,
            is_upperbound_inclusive=False,
        )
    elif op1 == "~":
        assert version2 is None
        if m.group("version1").isdigit():
            # only contains major version
            upperbound = lowerbound.bump_major()
        else:
            upperbound = lowerbound.bump_minor()
        spec = VersionSpec(
            lowerbound=lowerbound,
            upperbound=upperbound,
            is_lowerbound_inclusive=True,
            is_upperbound_inclusive=False,
        )
    elif op1 == "==":
        assert version2 is None
        upperbound = lowerbound
        spec = VersionSpec(
            lowerbound=lowerbound,
            upperbound=upperbound,
            is_lowerbound_inclusive=True,
            is_upperbound_inclusive=True,
        )
    else:
        upperbound = parse_version(version2) if version2 is not None else None
        if op1 == "<" or op1 == "<=":
            op1, op2 = op2, op1
            lowerbound, upperbound = upperbound, lowerbound
        spec = VersionSpec(
            lowerbound=lowerbound,
            upperbound=upperbound,
            is_lowerbound_inclusive=op1 == ">=",
            is_upperbound_inclusive=op2 == "<=",
        )
    return spec


def parse_version(version: str) -> semver.VersionInfo:
    m = re.match(
        r"^(?P<major>\d+)(?P<minor>\.\d+)?(?P<patch>\.\d+)?(?P<rest>[^\d].*)?$",
        version,
    )
    assert (
        m is not None
    ), f"Current parser is not able to parse version: `{version}` yet"

    if m is not None:
        parts = [
            m.group("major"),
            m.group("minor") or ".0",
            m.group("patch") or ".0",
            m.group("rest") or "",
        ]
        if not parts[-1].startswith("-") and parts[-1] != "":
            # add hyphen to make it compatible with semver package.
            # e.g. 21.11b1 -> 21.11.0-b1
            parts[-1] = "-" + parts[-1]
        version = "".join(parts)

    return semver.VersionInfo.parse(version)
