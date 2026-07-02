import ast
import os
import re
from abc import ABC, abstractmethod


class CodeSafetyAnalyzer(ABC):
    """One responsibility: score generated code for safety drift. 0.0 = clean, 1.0 = dangerous."""

    @abstractmethod
    def score(self, code: str) -> float: ...


class PythonASTAnalyzer(CodeSafetyAnalyzer):
    """Parses the generated Python into an AST and walks every node — no substring guessing."""

    _FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
    _FORBIDDEN_MODULES = {"subprocess", "ctypes", "importlib"}
    _FORBIDDEN_ATTRS = {"system", "popen", "call", "run", "Popen", "getoutput"}

    def score(self, code: str) -> float:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return 0.0  # unparseable output — trust decay handles the content side

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in self._FORBIDDEN_CALLS:
                    return 1.0
                if isinstance(node.func, ast.Attribute) and node.func.attr in self._FORBIDDEN_ATTRS:
                    return 1.0
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = node.module.split(".")[0] if isinstance(node, ast.ImportFrom) and node.module else ""
                names = {alias.name.split(".")[0] for alias in node.names} | ({module} if module else set())
                if names & self._FORBIDDEN_MODULES:
                    return 1.0

        return 0.0


class CppStaticAnalyzer(CodeSafetyAnalyzer):
    """
    Regex scan for patterns that have no place in a C++ low-latency / HFT hot path.
    Shell escapes, blocking I/O, hardcoded IPs, and naked malloc are the four failure modes.
    """

    # Shell escapes and blocking calls — latency killers and exfil vectors in generated C++.
    _BLOCKING_OR_ESCAPE = re.compile(
        r"\b(system|popen|execv?p?e?|sleep|usleep|nanosleep|printf|fprintf)\s*\("
    )
    # Hardcoded IPv4 — likely a command-and-control or data exfil address baked into the code.
    _HARDCODED_IP = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
    # malloc without a corresponding free — in HFT a memory leak is a slow position bleed.
    _NAKED_MALLOC = re.compile(r"\bmalloc\s*\(")
    _FREE_PRESENT = re.compile(r"\bfree\s*\(")

    def score(self, code: str) -> float:
        if self._BLOCKING_OR_ESCAPE.search(code):
            return 1.0
        if self._HARDCODED_IP.search(code):
            return 1.0
        if self._NAKED_MALLOC.search(code) and not self._FREE_PRESENT.search(code):
            return 0.8  # high drift — free may live in a destructor, so not max
        return 0.0


class SqlInjectionAnalyzer(CodeSafetyAnalyzer):
    """
    Catches SQL queries built by string interpolation instead of parameterized bindings.
    f-strings, .format(), and % substitution into SQL are the three patterns that matter.
    """

    # f"SELECT ... {user_input} ..." — direct injection path.
    _FSTRING_SQL = re.compile(
        r'f["\'].*?\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|WHERE)\b.*?["\']',
        re.IGNORECASE,
    )
    # query = "SELECT * FROM users WHERE id = " + user_id  — concatenation path.
    _CONCAT_SQL = re.compile(
        r'\b(SELECT|INSERT|UPDATE|DELETE|DROP)\b.*?(\+|\.format\s*\(|%\s*[({"\'])',
        re.IGNORECASE | re.DOTALL,
    )

    def score(self, code: str) -> float:
        if self._FSTRING_SQL.search(code):
            return 1.0
        if self._CONCAT_SQL.search(code):
            return 0.9
        return 0.0


def build_analyzer_from_env() -> CodeSafetyAnalyzer:
    """One env var swaps the entire code safety analysis strategy — mirrors build_provider_from_env."""
    kind = os.getenv("CODE_ANALYZER", "python").lower()
    analyzers = {
        "python": PythonASTAnalyzer,
        "cpp": CppStaticAnalyzer,
        "sql": SqlInjectionAnalyzer,
    }
    analyzer_class = analyzers.get(kind, PythonASTAnalyzer)
    return analyzer_class()
