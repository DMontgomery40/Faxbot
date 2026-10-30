"""Offline source contract for emitted console addresses (issue #59).

Run with unittest directly when the application dependencies are unavailable.
Compatibility inputs live in navigation.tsx and docsLinks.ts; they are not emitters.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
NAV = ROOT / 'api/admin_ui/src/navigation.tsx'
AREAS = 'overview|savings|faxes|delivery|recipients|admin|numbers|providers|costs|access|system'
ADDRESS = re.compile(r"[\"'`](?:\#/)?((?:" + AREAS + r")/[a-z-]+)(?=[?\"'`])")
LEGACY_CALL = re.compile(r"(?:onNavigate|navigate)(?:\?\.)?\(\s*['\"](send|jobs|inbox|settings|keys|diagnostics|routes|email|trunk|setup)['\"]")


def navigation_pages(text):
    body = text.split('export const NAVIGATION:')[1].split('// Who may see')[0]
    starts = list(re.finditer(r"^    id: '([a-z]+)', label:.*", body, re.M))
    pages = set()
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        section = body[start.end():end]
        ids = re.findall(r"\{ id: '([a-z-]+)', label:", section)
        ids += re.findall(r"providerPage\('([a-z-]+)'", section)
        pages.update(f'{start[1]}/{page}' for page in ids)
    return pages


def emitted_sources():
    for directory in ('api/app', 'api/admin_ui/src'):
        for path in sorted((ROOT / directory).rglob('*')):
            if path.suffix not in {'.py', '.ts', '.tsx'} or {'test', '__tests__'} & set(path.parts):
                continue
            text = path.read_text(encoding='utf-8')
            if path == NAV:
                # Keep bookmark compatibility, but check the actual navigation links.
                text = text[text.index('export const NAVIGATION:'):]
            if path.name == 'docsLinks.ts':
                # Historical documentation URLs are accepted inputs, not console destinations.
                text = text.split('const CURATED_PATHS:')[0]
            yield path.relative_to(ROOT), text


class ConsoleAddresses(unittest.TestCase):
    def test_emitted_addresses_resolve_without_aliases(self):
        pages = navigation_pages(NAV.read_text())
        self.assertEqual({page.split('/')[0] for page in pages},
                         {'overview', 'savings', 'faxes', 'delivery', 'recipients', 'admin'})
        errors = []
        for path, text in emitted_sources():
            for match in ADDRESS.finditer(text):
                if match[1] not in pages:
                    errors.append(f'{path}: {match[1]}')
            for line in text.splitlines():
                if re.search(r'\bdestination:', line):
                    for value in re.findall(r"['\"](send|jobs|inbox|settings|keys|diagnostics|routes|email|trunk|setup)['\"]", line):
                        errors.append(f'{path}: legacy destination {value}')
            for match in LEGACY_CALL.finditer(text):
                errors.append(f'{path}: legacy navigation call {match[1]}')
        self.assertEqual(errors, [], '\n'.join(errors))

    def test_parser_includes_conditional_providers_and_rejects_unknown_pages(self):
        pages = navigation_pages(NAV.read_text())
        self.assertIn('delivery/telnyx-fax', pages)
        self.assertNotIn('delivery/not-a-page', pages)
        self.assertNotIn('providers/trunk', pages)

    def test_every_shared_fixture_destination_is_canonical(self):
        import json
        pages = navigation_pages(NAV.read_text())
        def check(value):
            if isinstance(value, dict):
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)
            elif isinstance(value, str) and ADDRESS.match(repr(value)):
                self.assertIn(value.partition('?')[0], pages)
        for name in ('savingsMap.json', 'capabilities.json', 'overviewAttention.json'):
            check(json.loads((ROOT / 'api/admin_ui/src/__tests__' / name).read_text()))

    def test_intake_descriptions_state_the_direction_for_each_source_kind(self):
        # Execute the actual pure formatter without importing the HTTP server or providers.
        path = ROOT / 'api/app/intake/sources/http.py'
        tree = ast.parse(path.read_text())
        formatter = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_what')
        namespace = {}
        exec(compile(ast.Module(body=[formatter], type_ignores=[]), str(path), 'exec'), namespace)
        for kind in ('email', 'folder'):
            for direction, expected in (('receive', 'Imports incoming documents'), ('send', 'Submits outgoing faxes')):
                source = SimpleNamespace(kind=kind, direction=direction)
                self.assertTrue(namespace['_what'](source).startswith(expected))



if __name__ == '__main__':
    unittest.main()
