"""Guard: server/providers.json must equal scripts/init_fleet.get_providers().

The hosted contributor console offers the provider catalog via
GET /api/providers, served from server/providers.json — a duplicate of the
wizard's table, because the server image is self-contained and cannot import
scripts/ (see server/CLAUDE.md). This test is the drift alarm: change the
PROVIDERS table in scripts/init_fleet.py and you must regenerate the JSON:

    python3 -c "import sys, json; sys.path.insert(0, 'scripts'); \
import init_fleet; open('server/providers.json', 'w').write(\
json.dumps(init_fleet.get_providers(), indent=2) + '\\n')"

Self-running: `python scripts/test_provider_catalog_parity.py`.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def test_catalog_parity():
    import init_fleet
    wizard = init_fleet.get_providers()
    served = json.loads((ROOT / "server" / "providers.json").read_text())
    assert wizard == served, (
        "server/providers.json is out of date with init_fleet.PROVIDERS — "
        "regenerate it (see this test's docstring)."
    )
    print(f"PASS test_catalog_parity ({len(served)} providers)")


if __name__ == "__main__":
    test_catalog_parity()
    print("ALL PASS")
