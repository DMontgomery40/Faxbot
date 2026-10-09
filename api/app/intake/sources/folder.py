"""A watched folder: wait until a file stops changing, read it, then move it to done/ or failed/.

A file is read only when two looks at least ``settle`` seconds apart found the
same size and modification time, and that time is itself at least ``settle``
seconds old, so a scanner still writing (or a copy that keeps the original
time) is never read half-written. Files starting with a dot, folders,
symbolic links and other file types are left alone. A processed file moves to
``done/``; one that could not be used moves to ``failed/`` with a
``<name>.reason.txt`` beside it saying why. A file with the same name already
there is kept and the new one gets a number. A crash before the move reads the
file again, and the connector finds its identity already recorded.
"""
from dataclasses import dataclass
import os
import threading
import time


DOCUMENTS = {'.pdf': 'pdf', '.tif': 'tiff', '.tiff': 'tiff'}
SIDECAR = '.json'
DONE, FAILED = 'done', 'failed'


@dataclass(frozen=True)
class Found:
    name: str
    path: str
    kind: str
    size: int
    stable_since: float  # monotonic time it was first seen unchanged


class FolderError(RuntimeError):
    pass


class Watch:
    """Remembers what each folder looked like, per connector, between checks (in memory)."""

    def __init__(self, *, clock=time.monotonic, wall=time.time):
        self.clock, self.wall = clock, wall
        self._seen = {}
        self._lock = threading.Lock()

    def _signature(self, path):
        info = os.lstat(path)
        import stat
        if not stat.S_ISREG(info.st_mode):
            return None
        return info.st_size, info.st_mtime_ns

    def settled(self, source_id, path, settle):
        """(stable, size, first time it was seen unchanged); a changed file starts its wait again."""
        try:
            signature = self._signature(path)
        except OSError:
            return False, 0, 0.0
        if signature is None:
            return False, 0, 0.0
        now = self.clock()
        key = (source_id, path)
        with self._lock:
            previous = self._seen.get(key)
            if previous is None or previous[0] != signature:
                self._seen[key] = (signature, now)
                return False, signature[0], now
            first = previous[1]
        old_enough = self.wall() - signature[1] / 1e9 >= settle
        return (now - first >= settle and old_enough and signature[0] > 0), signature[0], first

    def forget(self, source_id, path):
        with self._lock:
            self._seen.pop((source_id, path), None)


def check(path):
    """Raise FolderError(kind) when the folder is missing ('missing') or not readable and writable ('access')."""
    if not os.path.isdir(path) or os.path.islink(path):
        raise FolderError('missing')
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise FolderError('access')


def documents(path):
    """Document files waiting in the folder, by name."""
    found = []
    try:
        names = sorted(os.listdir(path))
    except OSError:
        raise FolderError('access') from None
    for name in names:
        if name.startswith('.'):
            continue
        suffix = os.path.splitext(name)[1].casefold()
        if suffix in DOCUMENTS:
            found.append((name, os.path.join(path, name), DOCUMENTS[suffix]))
    return found


def sidecar_for(path, name):
    """The sidecar beside a document (``scan.pdf`` and ``scan.json``), or None."""
    stem = os.path.splitext(name)[0]
    candidate = os.path.join(path, stem + SIDECAR)
    return candidate if os.path.isfile(candidate) and not os.path.islink(candidate) else None


def read(path, limit):
    with open(path, 'rb') as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise FolderError('large')
    return data


def _unique(directory, name):
    target = os.path.join(directory, name)
    stem, suffix = os.path.splitext(name)
    number = 2
    while os.path.exists(target):
        target = os.path.join(directory, f'{stem} ({number}){suffix}')
        number += 1
    return target


def move(root, path, *, failed=False, reason=None, sidecar=None):
    """Move a processed file (and its sidecar) into done/ or failed/; a failed one gets its reason file."""
    directory = os.path.join(root, FAILED if failed else DONE)
    os.makedirs(directory, exist_ok=True)
    target = _unique(directory, os.path.basename(path))
    os.replace(path, target)
    if sidecar and os.path.exists(sidecar):
        os.replace(sidecar, _unique(directory, os.path.splitext(os.path.basename(target))[0] + SIDECAR))
    if failed and reason:
        with open(_unique(directory, os.path.basename(target) + '.reason.txt'), 'w', encoding='utf-8') as handle:
            handle.write(reason.strip() + '\n')
    return target
