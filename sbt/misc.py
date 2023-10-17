import functools
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Union

from typing_extensions import TypedDict


class ExecProcessError(Exception):
    pass


class InvalidPackageError(Exception):
    pass


class IncompatibleDependencyError(Exception):
    pass


NewEnvVar = TypedDict("NewEnvVar", name=str, value=str)


def stdout(line):
    """Print line to stdout"""
    print(line)


def exec(
    cmd: Union[str, Sequence[Union[str, Path]]],
    handler: Optional[Callable[[str], None]] = None,
    check_returncode: bool = True,
    cwd: Union[Path, str] = "./",
    redirect_stderr: bool = False,
    capture_stdout: bool = True,
    env: Optional[Union[List[str], List[Union[str, NewEnvVar]], dict]] = None,
) -> List[str]:
    """
    Execute a command and return the list of lines, in which the newline character is stripped away.

    Args:
        cmd: Command to execute.
        handler: function to process each line of the output.
        check_returncode: Whether to check the return code.
        cwd: working directory.
        redirect_stderr: Whether to redirect stderr to stdout.
        capture_stdout: Whether to capture stdout. For running poetry command, we may not want to capture because they manipulate output lines.
        env: the environment variables to use in this process.
            - None is use the default behavior of Popen
            - a list of strings/dictionaries:
                - if the item is a string, it is the environment variable to pass from the parent process
                - if the item is a dictionary, it is the new environment variable to set, has the following format: { name: <name>, value: <value> }
            - a dictionary will be the environment variables to use
    """
    if isinstance(cmd, str):
        cmd = [x for x in cmd.split(" ") if x != ""]
    else:
        cmd = [str(x) for x in cmd]

    if handler is None:
        handler = lambda x: None

    if env is not None:
        if isinstance(env, list):
            tmp = {}
            for item in env:
                if isinstance(item, str):
                    if item in os.environ:
                        tmp[item] = os.environ[item]
                else:
                    assert isinstance(item, dict)
                    tmp[item["name"]] = item["value"]
            env = tmp

    if capture_stdout:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if redirect_stderr else None,
            cwd=str(cwd),
            env=env,
        )
        output = []

        while True:
            assert p.stdout is not None
            line = p.stdout.readline().decode("utf-8")
            if line != "":
                assert line[-1] == "\n"
                line = line[:-1]
                output.append(line)
                handler(line)
            elif p.poll() is not None:
                break

        returncode = p.returncode
    else:
        returncode = subprocess.call(cmd, cwd=str(cwd), env=env)
        output = []

    if check_returncode and returncode != 0:
        msg = (
            f"Command: f{cmd} returns non-zero exit status {returncode}\n"
            + "\n".join(output)
        )
        raise ExecProcessError(msg)

    return output


def cache_func():
    d = {}

    def wrapper(func):
        @functools.wraps(func)
        def fn(*args):
            if args not in d:
                d[args] = func(*args)
            return d[args]

        return fn

    return wrapper


def cache_method():
    """Cache instance's method during its life-time.
    Note: Order of the arguments is important. Different order of the arguments will result in different cache key.
    """

    def wrapper(func):
        fn_name = func.__name__

        @functools.wraps(func)
        def fn(self, *args, **kwargs):
            if not hasattr(self, "_cache"):
                self._cache = {}
            k = (
                fn_name,
                args,
                tuple(sorted(f"kw" + str(arg) for kw, arg in kwargs.items())),
            )
            if k not in self._cache:
                self._cache[k] = func(self, *args)
            return self._cache[k]

        return fn

    return wrapper


@contextmanager  # type: ignore
def mask_file(
    file_path: Union[str, Path],
):
    """Temporary mask out a file

    Arguments:
        file_path: The path to the file to mask
    """
    file_path = str(file_path)
    if not os.path.exists(file_path):
        try:
            yield None
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
    else:
        assert os.path.isfile(file_path)
        try:
            os.rename(file_path, file_path + ".tmp")
            yield None
        finally:
            os.rename(file_path + ".tmp", file_path)


def venv_path(
    name: str,
    loc: Path,
    virtualenv_dir: Path | str = "./venv",
    python_path: str = sys.executable,
):
    """Get virtual environment path of the package, create it if doesn't exist.

    For a package manager that manages virtualenvs in different location, it should override this function.

    Arguments:
        name: name of the package
        dir: directory of the package
    """
    path = (loc / virtualenv_dir).absolute()
    if not path.exists():
        exec(f"{python_path} -m venv {path}", env=["PATH"])

    if not path.exists():
        raise Exception(f"Environment path {path} doesn't exist")
    return Path(path)
