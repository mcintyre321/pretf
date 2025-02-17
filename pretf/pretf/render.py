import inspect
import os
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path, PurePath
from typing import Any, Callable, Dict, Generator, List, Optional, Union

from . import log
from .exceptions import FunctionNotFoundError
from .util import find_workflow_path, import_file
from .variables import (
    TerraformVariableStore,
    VariableProxy,
    VariableValue,
    get_variable_definitions_from_block,
)


class Block(Iterable):
    def __init__(self, block_type: str, labels: List[str], body: Any):
        self._block_type = block_type
        self._labels = labels
        self._body = body

    def __iter__(self) -> Generator[tuple, None, None]:
        if self._labels:
            result: dict = {}
            here = result
            for label in self._labels[:-1]:
                here[label] = {}
                here = here[label]
            here[self._labels[-1]] = self._body
        else:
            result = self._body
        yield (self._block_type, result)

    def _get_expression(self, name: Optional[str] = None) -> Union["Interpolated", str]:
        if self._block_type == "resource":
            parts = list(self._labels)
        elif self._block_type == "variable":
            parts = ["var"] + self._labels
        elif self._block_type == "provider":
            parts = list(self._labels)
            if name == "alias":
                if self._body:
                    alias = self._body.get("alias")
                    if alias:
                        parts.append(alias)
                else:
                    parts.append("default")
                return ".".join(parts)
        elif self._block_type == "locals":
            parts = ["local"]
        else:
            parts = [self._block_type] + list(self._labels)

        if name:
            parts.append(name)

        return Interpolated(".".join(parts))

    def __getattr__(self, name: str) -> Union["Interpolated", str]:
        return self._get_expression(name)

    __getitem__ = __getattr__

    def __repr__(self) -> str:
        parts = [self._block_type]
        parts.extend(self._labels)
        if self._body is not None:
            parts.append(self._body)
        return f"block({', '.join(repr(part) for part in parts)})"

    def __str__(self) -> str:
        return str(self._get_expression())


class Interpolated:
    def __init__(self, value: str):
        self.__value = value

    def __eq__(self, other: Any) -> bool:
        return str(self) == other

    def __getattr__(self, attr: str) -> "Interpolated":
        return type(self)(self.__value + "." + attr)

    def __getitem__(self, index: int) -> "Interpolated":
        return type(self)(f"{self.__value}[{index}]")

    def __repr__(self) -> str:
        return f"Interpolated({repr(self.__value)})"

    def __str__(self) -> str:
        return "${" + self.__value + "}"


class PathProxy:
    def __init__(self) -> None:
        self.cwd = Path.cwd()
        self.module = Path(".")
        self.root = Path(".")

    @property  # type: ignore
    @lru_cache(maxsize=None)
    def top(self) -> Path:
        """
        Returns the directory containing the pretf.workflow.py file,
        or the current directory if there is none.

        """

        workflow_path = find_workflow_path()
        if workflow_path:
            return workflow_path.parent
        else:
            return self.cwd


class Renderer:
    def __init__(self, files_to_create: Dict[Path, Path]):
        # These are all of the files that will be created.
        self.files_to_create = files_to_create

        # Variables will be populated from environment variables,
        # command line arguments, and files, as per standard Terraform
        # behaviour. They will also be populated as files get created.
        self.variables = TerraformVariableStore(
            files_to_create=files_to_create, process_jobs=self.process_jobs
        )

        # These are all of the jobs to create files.
        self.jobs: List[RenderJob] = []
        for target_path, source_path in self.files_to_create.items():
            job = RenderJob(
                source_path=source_path,
                target_path=target_path,
                variables=self.variables,
            )
            self.jobs.append(job)

        # This will be populated with blocks from each file being created.
        self.done: List[RenderJob] = []

    def process_jobs(self, until: Optional[str] = None) -> None:
        while self.jobs:
            if until and until in self.variables:
                break
            job = self.jobs.pop()
            try:
                done = job.run()
            except Exception:
                log.bad(f"create: {job.target_name} could not be processed")
                raise
            if done:
                self.done.append(job)
            else:
                self.jobs.append(job)

    def render(self) -> Dict[Path, Union[dict, List[dict]]]:
        self.process_jobs()
        results = {}
        for job in self.done:
            results[job.target_path] = job.contents()
        return results


class RenderJob:
    def __init__(
        self, source_path: Path, target_path: Path, variables: TerraformVariableStore
    ):

        self.source_path = source_path
        self.target_path = target_path
        self.target_name = target_path.name
        self.variables = variables

        self.done = False
        self.is_tfvars = self.target_name.endswith(".tfvars.json")
        self.return_value = None

        # Load the file and start the generator.
        with import_file(source_path) as module:

            if self.is_tfvars:
                func_name = "pretf_variables"
            else:
                func_name = "pretf_blocks"

            if not hasattr(module, func_name):
                raise FunctionNotFoundError(
                    f"create: {source_path} does not have a {repr(func_name)} function"
                )

            # Call the pretf_* function, passing in "path", "terraform" and "var" if required.
            var_proxy = variables.proxy(consumer=self.source_path)
            self.gen = call_pretf_function(
                func=getattr(module, func_name), var=var_proxy
            )

        self.blocks: List[dict] = []

    def contents(self) -> Union[dict, List[dict]]:
        if self.is_tfvars:
            merged = {}
            for block in self.blocks:
                for name, value in block.items():
                    merged[name] = value
            return merged
        else:
            return self.blocks

    def process_tf_block(self, block: dict) -> None:
        for var in get_variable_definitions_from_block(block, source=self.source_path):
            # Add the variable definition. This doesn't necessarily
            # make it available to use, because a tfvars file may
            # populate it later.
            self.variables.add(var)

    def process_tfvars_dict(self, values: dict) -> None:
        # Only populate the variable store with values in this file
        # if it is waiting for this file. It is possible to generate
        # tfvars files that don't get used as a source for values.
        if self.variables.tfvars_waiting_for(self.target_path):
            for name, value in values.items():
                var = VariableValue(name=name, value=value, source=self.source_path)
                self.variables.add(var)

    def run(self) -> bool:
        try:
            yielded = self.gen.send(self.return_value)
        except StopIteration:
            self.variables.file_created(self.target_path)
            return True

        self.return_value = yielded

        if self.is_tfvars:
            if not isinstance(yielded, dict):
                raise TypeError(f"expected dict to be yielded but got {repr(yielded)}")
            self.process_tfvars_dict(yielded)
            self.blocks.append(yielded)
        else:
            for block in unwrap_yielded(yielded):

                self.process_tf_block(block)
                self.blocks.append(block)

        return False


class TerraformProxy:
    @property  # type: ignore
    @lru_cache(maxsize=None)
    def workspace(self) -> str:
        workspace = os.getenv("TF_WORKSPACE")
        if not workspace:
            cwd = Path.cwd()
            try:
                workspace = (cwd / ".terraform" / "environment").read_text()
            except FileNotFoundError:
                workspace = "default"
        return workspace


def call_pretf_function(func: Callable, var: Optional[VariableProxy] = None) -> Any:
    kwargs: Dict[str, Any] = {}
    sig = inspect.signature(func)
    if "path" in sig.parameters:
        kwargs["path"] = PathProxy()
    if "terraform" in sig.parameters:
        kwargs["terraform"] = TerraformProxy()
    if "var" in sig.parameters and var is not None:
        kwargs["var"] = var
    return func(**kwargs)


def json_default(obj: Any) -> Any:
    if isinstance(obj, (Block, Interpolated, PurePath)):
        return str(obj)
    raise TypeError(repr(obj))


def unwrap_yielded(
    yielded: Union[Block, dict, Iterable], **kwargs: Any
) -> Generator[dict, None, None]:
    if isinstance(yielded, Block):
        yield dict(iter(yielded))
    elif isinstance(yielded, dict):
        yield yielded
    else:
        root = kwargs.get("root", yielded)
        parent = kwargs.get("parent", object())
        if isinstance(yielded, Iterable) and yielded != parent:
            for nested in yielded:
                yield from unwrap_yielded(nested, parent=yielded, root=root)
        else:
            raise TypeError(
                f"expected block to be yielded but got {repr(kwargs.get('root', yielded))}"
            )
