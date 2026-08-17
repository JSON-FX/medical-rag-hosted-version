"""ARCHITECTURE.md §3, enforced.

"`rag_core` — a plain Python package with no web framework and no provider SDKs
imported at module level. Everything provider-specific enters through a port."

The local build's version of this test checked for `django` alone, which is
about a third of what the sentence above asks for. These four checks are the
whole of it. They are cheap, and the thing they protect — a core that runs in
milliseconds with no network — is the stated payoff of the entire port
abstraction (ADR-001).
"""

import pathlib
import re

CORE = pathlib.Path(__file__).resolve().parents[2] / "src" / "rag_core"

WEB_FRAMEWORKS = ["django", "fastapi", "flask", "starlette", "uvicorn"]
PROVIDER_SDKS = [
    "google",
    "groq",
    "openai",
    "anthropic",
    "chromadb",
    "ollama",
    "httpx",
    "requests",
    "aiohttp",
]
DATABASE_DRIVERS = ["psycopg", "psycopg2", "asyncpg", "sqlalchemy", "sqlite3"]


def _offenders(modules: list[str]) -> list[str]:
    """Files in rag_core importing any of `modules`.

    Matches the module ROOT with a word boundary, not a substring: a bare
    substring match on a short name would hit half the standard library.
    """
    pattern = re.compile(
        r"^\s*(?:import|from)\s+(?:" + "|".join(re.escape(m) for m in modules) + r")\b",
        re.MULTILINE,
    )
    found = []
    for path in sorted(CORE.rglob("*.py")):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            found.append(f"{path.name}: {match.group(0).strip()}")
    return found


def test_the_scan_actually_finds_the_package():
    """A purity test that silently scans nothing passes vacuously, which is
    worse than not having one. Anchor off __file__ so the result does not
    depend on where pytest was invoked from."""
    assert CORE.is_dir(), f"rag_core not found at {CORE}"
    assert len(list(CORE.rglob("*.py"))) >= 8


def test_core_imports_no_web_framework():
    offenders = _offenders(WEB_FRAMEWORKS)
    assert offenders == [], f"rag_core must stay framework-free: {offenders}"


def test_core_imports_no_provider_sdk():
    """ "Everything provider-specific enters through a port." An SDK import here
    means a provider decision leaked below the adapter layer."""
    offenders = _offenders(PROVIDER_SDKS)
    assert offenders == [], f"rag_core must not import provider SDKs: {offenders}"


def test_core_imports_no_database_driver():
    offenders = _offenders(DATABASE_DRIVERS)
    assert offenders == [], f"rag_core must not import database drivers: {offenders}"


def test_core_never_imports_the_adapter_layer():
    """The dependency direction only ever points inward. Adapters import the
    core; the reverse is a cycle and the end of the abstraction."""
    offenders = _offenders(["rag_adapters"])
    assert offenders == [], f"rag_core must not import rag_adapters: {offenders}"


def test_core_declares_no_runtime_dependencies():
    """The purity checks above are per-module. This is the same claim made at
    the package level, where a reviewer actually looks."""
    pyproject = (CORE.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert "dependencies = []" in pyproject
