"""List the UI messages (templates and Python) and compare them with the catalogs.

    python tools/i18n_check.py            report messages missing from / unused in each catalog
    python tools/i18n_check.py --list     print every message once

The same extraction runs in tests/test_i18n.py, so a missing translation fails the tests.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

from jinja2 import Environment

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "email_sorter"
GETTEXT = {"_", "gettext", "ngettext"}


def template_messages() -> set[str]:
    env = Environment(extensions=["jinja2.ext.i18n"])
    found = set()
    for path in (PACKAGE / "web" / "templates").glob("*.html"):
        for _lineno, _func, message in env.extract_translations(path.read_text(encoding="utf-8")):
            # ngettext yields (singular, plural); the catalog is keyed by the singular
            found.add(message[0] if isinstance(message, tuple) else message)
    return {m for m in found if m}


def python_messages() -> set[str]:
    found = set()
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if name in GETTEXT:
                    found.add(node.args[0].value)
    return found


def messages() -> set[str]:
    return template_messages() | python_messages()


def main() -> int:
    wanted = messages()
    if "--list" in sys.argv:
        print("\n".join(sorted(wanted)))
        return 0
    code = 0
    for path in sorted((PACKAGE / "locale").glob("*.json")):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        missing, unused = sorted(wanted - set(catalog)), sorted(set(catalog) - wanted)
        print(f"{path.name}: {len(wanted)} messages, {len(missing)} missing, {len(unused)} unused")
        for m in missing:
            print("  missing:", m)
        for m in unused:
            print("  unused: ", m)
        code |= bool(missing)
    return code


if __name__ == "__main__":
    sys.exit(main())
