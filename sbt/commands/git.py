import os
from typing import Literal

import click
from loguru import logger

from sbt.config import PBTConfig
from sbt.vcs.git import Git


@click.command()
@click.option(
    "--repo",
    default="",
    help="Specify the multi-repository that we are working with. e.g., https://github.com/binh-vu/pbt",
)
@click.option("--cwd", default=".", help="Override current working directory")
@click.argument("command", type=click.Choice(["sync-dep"]))
def git(repo: str, cwd: str, command: Literal["sync-dep"]):
    cwd = os.path.abspath(cwd)

    cfg = PBTConfig.from_dir(cwd)
    if command == "sync-dep":
        if not cfg.library_path.exists() and len(cfg.dependency_repos) == 0:
            return

        cfg.library_path.mkdir(exist_ok=True, parents=True)

        sync_repos = set(cfg.dependency_repos)
        for subdir in cfg.library_path.iterdir():
            if not Git.is_git_dir(subdir):
                continue
            repo = Git.get_repo(subdir)
            if repo not in sync_repos:
                logger.warning(
                    "Found and skip a git directory in libraries that isn't in the list of dependencies: {}",
                    subdir,
                )
                continue
            sync_repos.remove(repo)
            logger.info("Pull dependency {}", repo)
            Git.pull(subdir, submodules=False)
        for repo in sync_repos:
            logger.info("Clone dependency {}", repo)
            Git.clone(repo, cfg.library_path)
    else:
        raise Exception(f"Invalid command: {command}")
