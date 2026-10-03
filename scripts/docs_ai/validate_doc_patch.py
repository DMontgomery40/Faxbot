#!/usr/bin/env python3
"""Validate the complete Git delta of an optional instructional prose proposal."""
import argparse
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile


def validate_staged(*, environment=None):
    # Disable rename detection so both a rename's deletion and addition must
    # independently satisfy the boundary. NUL delimiters preserve literal paths.
    result = subprocess.check_output([
        'git', 'diff', '--cached', '--raw', '-z', '--no-renames',
        '--no-ext-diff', '--no-textconv', 'HEAD', '--',
    ], env=environment)
    if not result:
        raise ValueError('Documentation proposal contains no changes.')
    records = result.split(b'\0')
    for position in range(0, len(records) - 1, 2):
        metadata = records[position].split()
        name = os.fsdecode(records[position + 1])
        candidate = PurePosixPath(name)
        if (len(metadata) != 5 or metadata[0] not in {b':000000', b':100644'}
                or metadata[1] not in {b'000000', b'100644'}
                or metadata[4] not in {b'A', b'M', b'D'}
                or candidate.suffix != '.md' or candidate.parts[0:1] != ('docs',)
                or candidate.name.casefold() in {'agents.md', 'claude.md', 'skill.md'}
                or '..' in candidate.parts or candidate.is_absolute()
                or candidate.parts[1:2] in [('generated',), ('architecture',)]):
            raise ValueError('Documentation proposal exceeds maintained Markdown scope.')


def validate(path):
    # Applying into a throwaway index resolves all patch formats using Git itself
    # without touching the checkout or its real staging area.
    path = str(Path(path).resolve())
    with tempfile.TemporaryDirectory(prefix='faxbot-docs-patch-') as temporary:
        environment = {**os.environ, 'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
        subprocess.run(['git', 'read-tree', 'HEAD'], env=environment, check=True)
        subprocess.run(['git', 'apply', '--cached', '--', path], env=environment, check=True)
        validate_staged(environment=environment)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('patch', nargs='?')
    parser.add_argument('--staged', action='store_true')
    options = parser.parse_args()
    if options.staged and options.patch is None:
        validate_staged()
    elif options.patch and not options.staged:
        validate(options.patch)
    else:
        parser.error('Supply either a patch path or --staged.')
