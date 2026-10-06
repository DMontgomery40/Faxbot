"""Screen paths in the guides name areas the console really has.

The console's navigation was reorganized (Tools, Settings and Jobs became Faxes, Numbers, Recipients,
Providers, Costs, Access and System), and guides kept naming the old areas for weeks because
Docs Autopilot checked labels but not paths. This check reads the top-level areas from
api/admin_ui/src/navigation.tsx and fails on any bold path such as **Tools → Delivery routes** whose
first step is not one of them. Pages inside an area are left to Docs Autopilot, since some are
named at run time (the carrier trunk page carries the carrier's name).
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
AREA = re.compile(r"^    id: '[^']+', label: '([^']+)'", re.MULTILINE)
PATH = re.compile(r"\*\*([A-Z][A-Za-z &]+) → ")
# Other products' screens that guides walk through, by page.
OTHER_PRODUCTS = {
    ('docs/setup/avaya.md', 'Line'),  # Avaya IP Office Manager
    ('docs/setup/sip-trunk.md', 'Account settings'),  # the Telnyx portal
}


def guides():
    for page in sorted((ROOT / 'docs').rglob('*.md')):
        name = page.relative_to(ROOT).as_posix()
        if not name.startswith('docs/generated/') and name != 'docs/release-notes.md':
            yield name, page.read_text()


def test_every_screen_path_starts_with_an_area_the_console_has():
    areas = set(AREA.findall((ROOT / 'api/admin_ui/src/navigation.tsx').read_text()))
    assert {'Faxes', 'Costs', 'System'} <= areas, areas
    wrong = [f'{name}:{number}: **{area} → …'
             for name, text in guides()
             for number, line in enumerate(text.splitlines(), 1)
             for area in PATH.findall(line)
             if area not in areas and (name, area) not in OTHER_PRODUCTS]
    assert not wrong, 'Guides name console areas that no longer exist:\n' + '\n'.join(wrong)
