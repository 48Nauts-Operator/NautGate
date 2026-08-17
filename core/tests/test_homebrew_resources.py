"""Release packaging checks that are cheap enough to keep in the normal suite."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _generator():
    path = Path(__file__).resolve().parents[2] / "packaging/homebrew/gen_resources.py"
    spec = importlib.util.spec_from_file_location("homebrew_resources", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resources_are_unique_and_target_python_312_macos():
    requirements = _generator().runtime_requirements()
    names = [name.lower() for name, _version in requirements]

    assert len(names) == len(set(names))
    assert "colorama" not in names  # Windows-only.
    assert names.count("pydantic") == 1  # The lock also carries a Python 3.14 pin.


def test_formula_generated_section_is_exactly_current():
    generator = _generator()
    generated, missing = generator.stanzas()
    assert missing == []

    current = generator.FORMULA.read_text()
    expected = generator.replace_generated_resources(current, generated + "\n")
    assert current == expected


def test_formula_service_has_a_persistent_default_database_url():
    formula = _generator().FORMULA.read_text()

    assert "revision 1" in formula
    assert (
        'NAUTGATE_DB_URL="${NAUTGATE_DB_URL:-postgres://$(id -un)@localhost:5432/nautgate}"'
        in formula
    )
    assert '"$(brew --prefix postgresql@16)/bin/createdb" nautgate' in formula
