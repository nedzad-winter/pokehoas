"""Tests for the Guide's type chart.

The frontend keeps its own copy of battle.TYPE_CHART (index.html, used for the
move badges, the matchup grid and the Guide's full chart). A silent drift would
show players wrong numbers, so this test parses the JS table and compares it.

Usage:  python3 tests/test_type_chart.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

os.environ["POKEMON_DATA_DIR"] = tempfile.mkdtemp(prefix="pokemon-typechart-test-")
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


html = (ROOT / "pokemon_encounter" / "www" / "index.html").read_text(encoding="utf-8")

types_match = re.search(r"const TYPES = \[(.*?)\];", html, re.S)
js_types = re.findall(r"'([a-z]+)'", types_match.group(1)) if types_match else []
check("Frontend TYPES lists all 18 types", len(js_types) == 18 and len(set(js_types)) == 18, js_types)
check("Frontend TYPES match the backend chart", set(js_types) == set(battle.TYPE_CHART))

chart_match = re.search(r"const TYPE_CHART = \{(.*?)\n\};", html, re.S)
js_chart: dict[str, dict[str, float]] = {}
for line in (chart_match.group(1) if chart_match else "").splitlines():
    row = re.match(r"\s*([a-z]+):\s*\{(.*)\},?\s*$", line)
    if row:
        js_chart[row.group(1)] = {k: float(v) for k, v in re.findall(r"([a-z]+):\s*([\d.]+)", row.group(2))}
check("Frontend TYPE_CHART has a row per type", set(js_chart) == set(battle.TYPE_CHART), sorted(js_chart))
for attack, row in battle.TYPE_CHART.items():
    check(f"TYPE_CHART row '{attack}' is identical in backend and frontend",
          js_chart.get(attack) == {k: float(v) for k, v in row.items()},
          (js_chart.get(attack), row))

check("Guide renders the type chart", "typeChartHtml()" in html and "function typeChartHtml" in html)

i18n_dir = ROOT / "pokemon_encounter" / "app" / "data" / "i18n"
for lang in ("en", "de"):
    strings = json.loads((i18n_dir / f"{lang}.json").read_text(encoding="utf-8"))
    keys = ["guide.typechart.title", "guide.typechart.p1", "guide.typechart.p2",
            "guide.typechart.attacker", "guide.typechart.defender"]
    check(f"[{lang}] type chart texts exist", all(strings.get(k) for k in keys))

print()
if failures:
    print(f"{len(failures)} test(s) failed: {failures}")
    sys.exit(1)
print("All tests passed.")
