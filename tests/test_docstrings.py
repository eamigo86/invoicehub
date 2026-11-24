"""AST-based docstring checks that ruff cannot enforce.

Covers three rules: no backticks in any docstring, a complete Args section on every public
callable with parameters, and no type annotations repeated inside Args entries.
"""

import ast
import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "migrations", ".git", "node_modules", "odd", "docs", "__pycache__"}
SECTION_RE = re.compile(r"^[A-Z][A-Za-z ]*:$")
ENTRY_RE = re.compile(r"^(\*{0,2}\w+)( \([^)]*\))?:")

Function = ast.FunctionDef | ast.AsyncFunctionDef


def is_public(name: str) -> bool:
    """Tell whether a name is public under the project standard.

    Args:
        name: Identifier of a function, method or class.

    Returns:
        True for names without a leading underscore and for dunder names.
    """
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


def parameter_names(node: Function) -> list[str]:
    """List the documented parameter names of a function.

    Args:
        node: Function definition to inspect.

    Returns:
        Names in signature order, star parameters prefixed, self and cls removed.
    """
    a = node.args
    names = [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    if a.vararg:
        names.append(f"*{a.vararg.arg}")
    if a.kwarg:
        names.append(f"**{a.kwarg.arg}")
    return [n for n in names if n not in {"self", "cls"}]


def args_entries(docstring: str) -> list[tuple[str, bool]] | None:
    """Parse the Args section of a docstring.

    Args:
        docstring: Raw docstring text.

    Returns:
        None when there is no Args section, otherwise one pair per entry made of the
        parameter name and whether the entry repeats a type in parentheses.
    """
    lines = docstring.expandtabs().splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "Args:":
            continue
        indent = len(line) - len(line.lstrip())
        entries: list[tuple[str, bool]] = []
        for nxt in lines[i + 1 :]:
            stripped = nxt.strip()
            nxt_indent = len(nxt) - len(nxt.lstrip())
            if stripped and nxt_indent <= indent and SECTION_RE.match(stripped):
                break
            match = ENTRY_RE.match(stripped)
            if stripped and nxt_indent == indent + 4 and match:
                entries.append((match.group(1), match.group(2) is not None))
        return entries
    return None


def _walk(node: ast.AST, in_function: bool, public_scope: bool) -> Iterator[tuple[ast.AST, bool]]:
    """Yield every documentable node with whether it is a public API member.

    Args:
        node: Node whose children are visited.
        in_function: Whether the node is nested inside a function.
        public_scope: Whether the enclosing scope is public.

    Yields:
        Pairs of module, class or function node and its public flag.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            public = public_scope and not in_function and is_public(child.name)
            yield child, public
            yield from _walk(
                child,
                in_function or not isinstance(child, ast.ClassDef),
                public,
            )
        else:
            yield from _walk(child, in_function, public_scope)


def check_source(source: str, filename: str = "<memory>") -> list[str]:
    """Check the docstring rules against Python source text.

    Args:
        source: Python source code.
        filename: Label used in violation messages.

    Returns:
        Human-readable violations; empty when the source is clean.
    """
    tree = ast.parse(source)
    violations: list[str] = []
    nodes: list[tuple[ast.AST, bool]] = [(tree, True), *_walk(tree, False, True)]
    for node, public in nodes:
        name = getattr(node, "name", "<module>")
        where = f"{filename}:{getattr(node, 'lineno', 1)} {name}"
        doc = ast.get_docstring(node, clean=False)  # type: ignore[arg-type]
        if doc and "`" in doc:
            violations.append(f"{where}: backtick in docstring")
        if not isinstance(node, Function) or not public:
            continue
        params = parameter_names(node)
        entries = args_entries(doc) if doc else None
        if params and entries is None:
            violations.append(f"{where}: missing Args section")
        for entry, typed in entries or []:
            if typed:
                violations.append(f"{where}: type repeated in Args entry {entry}")
        documented = {entry for entry, _ in entries or []}
        for param in params:
            if entries is not None and param not in documented:
                violations.append(f"{where}: parameter {param} not documented")
    return violations


def project_files() -> Iterator[Path]:
    """Iterate over every project Python file subject to the checks.

    Yields:
        Paths of Python files outside virtualenvs, caches and migrations.
    """
    for path in sorted(ROOT.rglob("*.py")):
        if not SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            yield path


def test_backtick_is_flagged() -> None:
    """A backtick in any docstring, even a private one, is a violation."""
    src = 'def _f():\n    """Uses `x`."""\n'
    assert any("backtick" in v for v in check_source(src))


def test_missing_args_section_is_flagged() -> None:
    """A public function with parameters needs an Args section."""
    src = 'def f(a):\n    """Do it."""\n'
    assert any("missing Args" in v for v in check_source(src))


def test_undocumented_parameter_is_flagged() -> None:
    """Every parameter, including star parameters, must be named in Args."""
    src = 'def f(a, *args, **kw):\n    """Do it.\n\n    Args:\n        a: First.\n    """\n'
    found = check_source(src)
    assert any("*args" in v for v in found)
    assert any("**kw" in v for v in found)


def test_typed_entry_is_flagged() -> None:
    """Repeating a type in an Args entry is a violation."""
    src = 'def f(a):\n    """Do it.\n\n    Args:\n        a (int): First.\n    """\n'
    assert any("type repeated" in v for v in check_source(src))


def test_clean_source_passes() -> None:
    """Complete docstrings, self and private helpers produce no violation."""
    src = (
        '"""Module."""\n\n'
        "class C:\n"
        '    """Class."""\n\n'
        "    def m(self, *args, **kwargs):\n"
        '        """Do it.\n\n'
        "        Args:\n"
        "            *args: Positional.\n"
        "            **kwargs: Keyword.\n"
        '        """\n\n'
        "    def _hidden(self, x):\n"
        "        pass\n"
    )
    assert check_source(src) == []


def test_no_violations_in_project() -> None:
    """Every project Python file satisfies the docstring rules."""
    violations: list[str] = []
    for path in project_files():
        violations += check_source(path.read_text(), str(path.relative_to(ROOT)))
    assert violations == []
