"""Every private Pydantic AI, Harness and Anthropic SDK name pcode relies on, in one place.

A release that moves or drops one fails here first, by name, rather than as
scattered behavior failures. Upgrading: fix what fails here, then run the suite.

The second half guards the quieter trap. A pcode subclass overriding an
upstream underscore method keeps working until a release removes or renames
that method; the override is then never called and nothing fails. So every
underscore method on such a subclass must override something, or be listed
below as pcode's own helper.
"""

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest

import pcode

UPSTREAM = ("pydantic_ai", "pydantic_ai_harness", "anthropic")

PRIVATE_IMPORTS = {
    ("anthropic._models", "construct_type"),
    ("pydantic_ai._run_context", "dispatch_event_stream"),
    ("pydantic_ai._utils", "PeekableAsyncStream"),
    ("pydantic_ai_harness.background_tools._capability", "_Completion"),
    ("pydantic_ai_harness.background_tools._capability", "_format_background_failure"),
    ("pydantic_ai_harness.background_tools._capability", "_format_background_result"),
    ("pydantic_ai_harness.compaction._shared", "find_token_cutoff"),
    ("pydantic_ai_harness.filesystem._content", "apply_replacements"),
    ("pydantic_ai_harness.playwright._toolset", "_without_credentials"),
    ("pydantic_ai_harness.repo_context._inventory", "scan_assets"),
    ("pydantic_ai_harness.repo_context._loader", "context_file_label"),
    ("pydantic_ai_harness.repo_context._loader", "discover_instruction_files"),
    ("pydantic_ai_harness.shell._policy", "recoverable"),
    ("pydantic_ai_harness.subagents._toolset", "_Ended"),
}

# Underscore methods pcode adds on its subclasses of upstream classes; anything
# else with a leading underscore there must override an upstream method.
OWN_HELPERS = {
    "pcode.background_delegation.BackgroundDelegation": {
        "_drain_while_waiting",
        "_ending",
        "_node",
        "_wait_step",
    },
    "pcode.filesystem.DisplayFileSystemToolset": {
        "_display_path",
        "_emit_change",
        "_recorded",
        "_take",
    },
    "pcode.isolated_delegation.WorkspaceSubAgentToolset": {
        "_add_model",
        "_answer_worker",
        "_delegate",
        "_message_worker",
        "_stop_worker",
    },
    "pcode.llm_proxy.ProxiedCodexProvider": {"_new_http_client"},
    "pcode.repo_context.AutomaticRepoContext": {
        "_inventory_context",
        "_load_inventory",
        "_on_search",
    },
    "pcode.shell_tools.JobShellToolset": {
        "_compile",
        "_emit_finished",
        "_handle",
        "_require",
        "_result",
        "_wait",
        "_wait_seconds",
    },
    "pcode.workspace_filesystem.WorkspaceFileSystem": {"_toolset_type"},
}
# Python's and Pydantic AI's own bookkeeping, not methods anyone overrides.
IGNORED = {"_abc_impl", "_is_protocol", "_registered_kind"}


def _private_imports() -> set[tuple[str, str]]:
    """(module, name) for each import in src/pcode of a private upstream module or name."""
    found = set()
    for path in Path(pcode.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module.split(".")[0] not in UPSTREAM:
                continue
            private_module = any(part.startswith("_") for part in node.module.split("."))
            for alias in node.names:
                if private_module or alias.name.startswith("_"):
                    found.add((node.module, alias.name))
    return found


def test_the_private_import_list_is_complete():
    assert _private_imports() == PRIVATE_IMPORTS


@pytest.mark.parametrize("module, name", sorted(PRIVATE_IMPORTS))
def test_each_private_name_still_exists(module, name):
    assert hasattr(importlib.import_module(module), name)


def _upstream_subclasses():
    for info in pkgutil.walk_packages(pcode.__path__, "pcode."):
        module = importlib.import_module(info.name)
        for name, cls in vars(module).items():
            if not inspect.isclass(cls) or cls.__module__ != module.__name__:
                continue
            if any(base.__module__.split(".")[0] in UPSTREAM for base in cls.__mro__[1:]):
                yield f"{cls.__module__}.{name}", cls


def test_every_private_method_overrides_upstream_or_is_a_listed_helper():
    own = {}
    for qualname, cls in _upstream_subclasses():
        added = {
            attr
            for attr in vars(cls)
            if attr.startswith("_")
            and not attr.startswith("__")
            and attr not in IGNORED
            and not any(hasattr(base, attr) for base in cls.__mro__[1:])
        }
        if added:
            own[qualname] = added
    assert own == OWN_HELPERS
