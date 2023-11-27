from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence

import orjson
from semver import VersionInfo


class PackageType(str, Enum):
    Poetry = "poetry"
    Maturin = "maturin"


@dataclass
class Package:
    name: str
    version: str
    location: Path
    type: PackageType

    include: list[str]

    dependencies: dict[str, DepConstraints]

    def find_manually_installed_dependencies(self, cache_dir: Path) -> list[Path]:
        """Find dependencies that were installed manually (via command line) into the package environment"""
        cache_file = self._get_manually_installed_dep_file(cache_dir)
        if not cache_file.exists():
            return []
        return [Path(loc) for loc in orjson.loads(cache_file.read_text())]

    def save_manually_installed_dependencies(
        self, cache_dir: Path, manually_installed_deps: Sequence[Path]
    ) -> None:
        """Save the list of dependencies of the package that were installed manually into the package environment"""
        self._get_manually_installed_dep_file(cache_dir).write_bytes(
            orjson.dumps(
                [str(x) for x in manually_installed_deps], option=orjson.OPT_INDENT_2
            )
        )

    def _get_manually_installed_dep_file(self, cache_dir: Path) -> Path:
        return cache_dir / "manually_installed_dependencies.json"


@dataclass(eq=True)
class DepConstraint:
    """Constraint of a dependency.

    The two important fields are rule (for comparing between versions) and constraint
    (for distinguish between different platforms/os).

    To reconstruct/save the constraint,
    back to the original package specification, update the `origin_spec` with the a new key
    stored in `version_field` and value from `version`. The reason for this behaviour is to
    support cases such as where the dependency is from git (`version_field` = 'git').
    """

    # rule for matching version of dependency, e.g. "^1.0.0" or ">= 1.0.0", the rule sometimes depends on what package's type
    version_spec: str
    # an identifier for the condition that this version is applicable to.
    # none mean there is no other constraint.
    constraint: Optional[str] = None
    # name of the rule field in origin specification
    # none if the spec is just a string
    version_spec_field: Optional[str] = None
    # the original specification without the version
    # none if the spec is just a string
    origin_spec: Optional[dict] = None


# see: https://python-poetry.org/docs/dependency-specification/
# the constraints always sorted by constraint
DepConstraints = list[DepConstraint]


@dataclass
class VersionSpec:
    lowerbound: Optional[VersionInfo]
    upperbound: Optional[VersionInfo]
    is_lowerbound_inclusive: bool
    is_upperbound_inclusive: bool

    def is_version_compatible(self, version: VersionInfo) -> bool:
        """Check if the given version is compatible with the given rule

        Args:
            version: the version to check
        """
        incompatible = (
            self.lowerbound is not None
            and (
                (self.is_lowerbound_inclusive and self.lowerbound > version)
                or (not self.is_lowerbound_inclusive and self.lowerbound >= version)
            )
        ) or (
            self.upperbound is not None
            and (
                (self.is_upperbound_inclusive and self.upperbound < version)
                or (not self.is_upperbound_inclusive and self.upperbound <= version)
            )
        )
        return not incompatible

    def intersect(self, version_spec: VersionSpec) -> VersionSpec:
        """Intersect two version specs. Result in a stricter version spec.

        Raise exception if the intersection is empty.

        Examples:
            - "^1.0.0" and "^2.0.0" -> Exception
            - "^1.1.0" and "^1.2.0" -> "^1.2.0"
        """
        lb = self.lowerbound
        is_lb_inclusive = self.is_lowerbound_inclusive

        if version_spec.lowerbound is not None:
            if lb is None:
                lb = version_spec.lowerbound
                is_lb_inclusive = version_spec.is_lowerbound_inclusive
            elif version_spec.lowerbound > lb:
                lb = version_spec.lowerbound
                is_lb_inclusive = version_spec.is_lowerbound_inclusive
            elif version_spec.lowerbound == lb:
                is_lb_inclusive = (
                    is_lb_inclusive and version_spec.is_lowerbound_inclusive
                )

        ub = self.upperbound
        is_ub_inclusive = self.is_upperbound_inclusive

        if version_spec.upperbound is not None:
            if ub is None:
                ub = version_spec.upperbound
                is_ub_inclusive = version_spec.is_upperbound_inclusive
            elif version_spec.upperbound < ub:
                ub = version_spec.upperbound
                is_ub_inclusive = version_spec.is_upperbound_inclusive
            elif version_spec.upperbound == ub:
                is_ub_inclusive = (
                    is_ub_inclusive and version_spec.is_upperbound_inclusive
                )

        if lb is not None and ub is not None and lb > ub:
            raise ValueError(
                "Can't intersect two version specs: {} and {} because it results in empty spec".format(
                    self, version_spec
                )
            )

        return VersionSpec(
            lowerbound=lb,
            upperbound=ub,
            is_lowerbound_inclusive=is_lb_inclusive,
            is_upperbound_inclusive=is_ub_inclusive,
        )

    def to_pep508_string(self):
        s = f">{'=' if self.is_lowerbound_inclusive else ''} {str(self.lowerbound)}"
        if self.upperbound is not None:
            s += f", <{'=' if self.is_upperbound_inclusive else ''} {str(self.upperbound)}"
        return s

    def __eq__(self, other: VersionSpec):
        if other is None or not isinstance(other, VersionSpec):
            return False

        return (
            (
                (
                    self.lowerbound is not None
                    and other.lowerbound is not None
                    and self.lowerbound == other.lowerbound
                )
                or (self.lowerbound is None and other.lowerbound is None)
            )
            and (
                (
                    self.upperbound is not None
                    and other.upperbound is not None
                    and self.upperbound == other.upperbound
                )
                or (self.upperbound is None and other.upperbound is None)
            )
            and (self.is_lowerbound_inclusive == other.is_lowerbound_inclusive)
            and (self.is_upperbound_inclusive == other.is_upperbound_inclusive)
        )
