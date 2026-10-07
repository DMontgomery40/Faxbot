"""The error-corrected, interleaved byte stream that payload pages carry.

The container is cut into ``n`` Reed-Solomon codewords of ``k = 255 - parity``
message symbols each (the last one zero-padded). The stream is written symbol
by symbol across codewords: stream byte ``j * n + i`` is symbol ``j`` of
codeword ``i``. Pages carry the stream in order, so any run of ``L`` lost
stream bytes (a damaged scan line, a dropped row, a lost page) takes at most
``ceil(L / n)`` symbols from each codeword, spread evenly. Lost bytes are
known (their row failed its check), so they are erasures, and a codeword
repairs up to ``parity`` of them, or half as many undetected errors.
"""
from . import rs

FEC_LEVELS = {'low': 16, 'medium': 32, 'high': 64}


class StreamError(ValueError):
    pass


def codeword_count(container_length, parity):
    k = rs.N - parity
    return max(1, -(-container_length // k))


def build(container, parity):
    """(stream bytes, codeword count) for a container."""
    k = rs.N - parity
    n = codeword_count(len(container), parity)
    padded = bytes(container) + bytes(n * k - len(container))
    columns = [padded[j::k] for j in range(k)]
    columns += rs.encode_columns(columns, parity)
    return b''.join(columns), n


def recover(stream, known, n, parity, container_length):
    """Repair the stream and return the container bytes.

    ``stream`` holds what was read (anything at unknown positions);
    ``known[t]`` is 1 when stream byte ``t`` was read from a row that passed
    its check. Raises StreamError when some codeword has too much damage.
    Returns (container, report) where report counts erasures and repairs.
    """
    k = rs.N - parity
    total = n * rs.N
    if len(stream) < total or len(known) < total:
        raise StreamError('stream shorter than its header states')
    stream = bytes(stream[:total])
    columns = [stream[j * n:(j + 1) * n] for j in range(rs.N)]
    unknown = [t for t in range(total) if not known[t]]
    erasures = {}
    for t in unknown:
        erasures.setdefault(t % n, []).append(t // n)
    syndromes = rs.syndromes_columns(columns, parity)
    flagged = set(erasures)
    for column in syndromes:
        if column.count(0) != n:
            flagged.update(i for i, value in enumerate(column) if value)
    repaired = {}
    worst = 0
    failed = 0
    for i in sorted(flagged):
        word = [column[i] for column in columns]
        positions = erasures.get(i, [])
        worst = max(worst, len(positions))
        try:
            repaired[i] = rs.correct(word, parity, positions)
        except rs.ReedSolomonError:
            failed += 1
    if failed:
        raise StreamError(f'{failed} of {n} codewords were too damaged to repair')
    message = bytearray()
    for j in range(k):
        column = bytearray(columns[j])
        for i, word in repaired.items():
            column[i] = word[j]
        message += column
    # message is column-major (symbol j of every codeword); put codewords back in order.
    container = bytearray(n * k)
    for j in range(k):
        container[j::k] = message[j * n:(j + 1) * n]
    report = {'codewords': n, 'erased_bytes': len(unknown), 'repaired_codewords': len(repaired),
              'worst_codeword_erasures': worst}
    return bytes(container[:container_length]), report
