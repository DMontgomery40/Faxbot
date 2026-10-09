"""Reed-Solomon RS(255, 255 - p) over GF(2^8) for the fax payload codec.

Field: primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11d), generator
alpha = 2, first consecutive root alpha^0 (fcr = 0). The generator polynomial
of a code with ``p`` parity symbols is prod_{i<p} (x - alpha^i). Codewords are
systematic, highest degree first: the k message symbols, then p parity
symbols. These are the parameters of the widely used ``reedsolo`` defaults and
of the "Reed-Solomon codes for coders" tutorial (Wikiversity), whose
errors-and-erasures decoder (Berlekamp-Massey with Forney syndromes, Chien
search and the Forney algorithm) this module follows. The browser decoder in
``tools/fax-decoder/decoder.js`` implements the same code; both are checked
against ``tests/fixtures/codec_rs_vectors.json``.

Many codewords are encoded and checked at once: column ``j`` holds symbol
``j`` of every codeword as one ``bytes`` object, multiplication by a constant
is ``bytes.translate`` and addition is an XOR of big integers, so the
per-symbol work runs in C. Only codewords whose syndromes are not zero, or
that carry erasures, are corrected one at a time in Python.
"""
from functools import lru_cache

PRIMITIVE = 0x11D
N = 255

EXP = [0] * 512
LOG = [0] * 256
_value = 1
for _power in range(255):
    EXP[_power] = _value
    LOG[_value] = _power
    _value <<= 1
    if _value & 0x100:
        _value ^= PRIMITIVE
for _power in range(255, 512):
    EXP[_power] = EXP[_power - 255]
del _value, _power


class ReedSolomonError(ValueError):
    """A codeword has more damage than its parity can repair."""


def gmul(a, b):
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def gdiv(a, b):
    if b == 0:
        raise ZeroDivisionError('division by zero in GF(256)')
    if a == 0:
        return 0
    return EXP[(LOG[a] + 255 - LOG[b]) % 255]


def gpow(a, power):
    return EXP[(LOG[a] * power) % 255]


def ginv(a):
    return EXP[255 - LOG[a]]


@lru_cache(maxsize=256)
def multiply_table(constant):
    """A 256-byte translation table: x -> constant * x."""
    return bytes(gmul(constant, value) for value in range(256))


def poly_scale(poly, factor):
    return [gmul(value, factor) for value in poly]


def poly_add(first, second):
    size = max(len(first), len(second))
    result = [0] * size
    for index, value in enumerate(first):
        result[index + size - len(first)] = value
    for index, value in enumerate(second):
        result[index + size - len(second)] ^= value
    return result


def poly_mul(first, second):
    result = [0] * (len(first) + len(second) - 1)
    for j, b in enumerate(second):
        if b == 0:
            continue
        log_b = LOG[b]
        for i, a in enumerate(first):
            if a:
                result[i + j] ^= EXP[LOG[a] + log_b]
    return result


def poly_eval(poly, x):
    if x == 0:
        return poly[-1]
    log_x = LOG[x]
    value = poly[0]
    for coefficient in poly[1:]:
        value = (EXP[LOG[value] + log_x] if value else 0) ^ coefficient
    return value


def poly_div(dividend, divisor):
    """Synthetic division by a monic divisor: (quotient, remainder)."""
    out = list(dividend)
    for i in range(len(dividend) - (len(divisor) - 1)):
        coefficient = out[i]
        if coefficient:
            for j in range(1, len(divisor)):
                if divisor[j]:
                    out[i + j] ^= gmul(divisor[j], coefficient)
    separator = -(len(divisor) - 1)
    return out[:separator], out[separator:]


@lru_cache(maxsize=32)
def generator(parity):
    poly = [1]
    for i in range(parity):
        poly = poly_mul(poly, [1, EXP[i]])
    return tuple(poly)


def _xor(first, second, size):
    return (int.from_bytes(first, 'little') ^ int.from_bytes(second, 'little')).to_bytes(size, 'little')


def encode_columns(columns, parity):
    """Parity columns for many codewords at once.

    ``columns[j]`` is symbol j of every codeword (all the same length). Returns
    ``parity`` columns in codeword order.
    """
    if not columns:
        raise ValueError('nothing to encode')
    size = len(columns[0])
    gen = generator(parity)
    zero = bytes(size)
    registers = [zero] * parity
    tables = [multiply_table(gen[t + 1]) for t in range(parity)]
    for column in columns:
        feedback = _xor(column, registers[0], size)
        registers = registers[1:] + [zero]
        if feedback.count(0) == size:
            continue
        registers = [_xor(registers[t], feedback.translate(tables[t]), size) for t in range(parity)]
    return registers


def syndromes_columns(columns, parity):
    """Syndromes S_0..S_{p-1} of every codeword, as ``parity`` columns."""
    size = len(columns[0])
    accumulators = []
    for i in range(parity):
        table = multiply_table(EXP[i])
        accumulator = bytes(size)
        for column in columns:
            accumulator = _xor(accumulator.translate(table), column, size)
        accumulators.append(accumulator)
    return accumulators


def encode_message(message, parity):
    """One systematic codeword (list of ints) for a short message (len + parity <= 255)."""
    if len(message) + parity > N:
        raise ValueError('message too long for one codeword')
    columns = [bytes([value]) for value in message]
    return list(message) + [column[0] for column in encode_columns(columns, parity)]


def _syndromes(codeword, parity):
    return [0] + [poly_eval(codeword, EXP[i]) for i in range(parity)]


def _errata_locator(coefficient_positions):
    locator = [1]
    for position in coefficient_positions:
        locator = poly_mul(locator, poly_add([1], [gpow(2, position), 0]))
    return locator


def _error_evaluator(syndromes, locator, parity):
    _, remainder = poly_div(poly_mul(syndromes, locator), [1] + [0] * (parity + 1))
    return remainder


def _correct_errata(codeword, syndromes, positions):
    coefficient_positions = [len(codeword) - 1 - position for position in positions]
    locator = _errata_locator(coefficient_positions)
    evaluator = _error_evaluator(syndromes[::-1], locator, len(locator) - 1)[::-1]
    roots = [gpow(2, position) for position in coefficient_positions]
    magnitudes = [0] * len(codeword)
    for i, root in enumerate(roots):
        root_inverse = ginv(root)
        derivative = 1
        for j, other in enumerate(roots):
            if j != i:
                derivative = gmul(derivative, 1 ^ gmul(root_inverse, other))
        if derivative == 0:
            raise ReedSolomonError('could not find an error magnitude')
        y = gmul(root, poly_eval(evaluator[::-1], root_inverse))
        magnitudes[positions[i]] = gdiv(y, derivative)
    return [a ^ b for a, b in zip(codeword, magnitudes)]


def _error_locator(syndromes, parity, erasure_count):
    locator, previous = [1], [1]
    shift = len(syndromes) - parity
    for i in range(parity - erasure_count):
        k = i + shift
        delta = syndromes[k]
        for j in range(1, len(locator)):
            delta ^= gmul(locator[-(j + 1)], syndromes[k - j])
        previous = previous + [0]
        if delta:
            if len(previous) > len(locator):
                new = poly_scale(previous, delta)
                previous = poly_scale(locator, ginv(delta))
                locator = new
            locator = poly_add(locator, poly_scale(previous, delta))
    while locator and locator[0] == 0:
        del locator[0]
    errors = len(locator) - 1
    if errors * 2 + erasure_count > parity:
        raise ReedSolomonError('too many errors to correct')
    return locator


def _find_errors(locator_reversed, length):
    errors = len(locator_reversed) - 1
    if errors == 0:
        return []
    positions = [length - 1 - i for i in range(length) if poly_eval(locator_reversed, gpow(2, i)) == 0]
    if len(positions) != errors:
        raise ReedSolomonError('the error locator does not match the damage')
    return positions


def _forney_syndromes(syndromes, positions, length):
    reversed_positions = [length - 1 - position for position in positions]
    forney = list(syndromes[1:])
    for position in reversed_positions:
        x = gpow(2, position)
        for j in range(len(forney) - 1):
            forney[j] = gmul(forney[j], x) ^ forney[j + 1]
    return forney


def correct(codeword, parity, erasures=(), syndromes=None):
    """Repair one codeword (list of ints, length <= 255) in place of erasures and errors.

    ``2 * errors + erasures <= parity`` is always repaired. Returns the
    corrected codeword; raises ReedSolomonError when the damage is too great.
    """
    word = list(codeword)
    erasures = sorted(set(erasures))
    if len(erasures) > parity:
        raise ReedSolomonError('too many erasures to correct')
    for position in erasures:
        word[position] = 0
    if syndromes is None or erasures:
        syndromes = _syndromes(word, parity)
    else:
        syndromes = [0] + list(syndromes)
    if max(syndromes) == 0:
        return word
    forney = _forney_syndromes(syndromes, erasures, len(word))
    locator = _error_locator(forney, parity, len(erasures))
    errors = _find_errors(locator[::-1], len(word))
    word = _correct_errata(word, syndromes, list(erasures) + errors)
    if max(_syndromes(word, parity)) != 0:
        raise ReedSolomonError('could not correct the codeword')
    return word
