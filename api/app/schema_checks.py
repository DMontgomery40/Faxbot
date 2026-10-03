"""Fail-closed parser for the small, frozen access CHECK expression grammar.

This is structural comparison, not general SQL equivalence. Only PostgreSQL's
reviewed implicit ::text casts on VARCHAR columns/string literals disappear.
Boolean grouping and precedence, column names, and literal values remain exact.
"""
import re

import sqlalchemy as sa


_TOKEN = re.compile(r"\s*(?:(?P<string>'(?:''|[^'])*')|(?P<integer>[0-9]+)|"
                    r"(?P<word>[A-Za-z_][A-Za-z_0-9]*)|(?P<symbol>::|<>|>=|<=|[=()]))")
_BOOLEAN = {'AND', 'OR', 'IS', 'NOT', 'NULL'}


def canonical_check(expression, columns):
    """Return a bounded immutable AST, or reject any unreviewed SQL spelling."""
    if not isinstance(expression, str) or len(expression) > 16384:
        raise ValueError('Unsupported frozen CHECK expression')
    tokens, position = [], 0
    while position < len(expression):
        if not expression[position:].strip():
            break
        match = _TOKEN.match(expression, position)
        if match is None:
            raise ValueError('Unsupported frozen CHECK expression')
        kind, value = match.lastgroup, match.group(match.lastgroup)
        tokens.append((kind, value))
        position = match.end()
        if len(tokens) > 2048:
            raise ValueError('Unsupported frozen CHECK expression')
    parser = _Parser(tokens, columns)
    try:
        result = parser.boolean_or()
        if parser.position != len(tokens) or not parser.is_boolean(result):
            raise ValueError('Unsupported frozen CHECK expression')
        return result
    except RecursionError:
        raise ValueError('Unsupported frozen CHECK expression') from None


class _Parser:
    def __init__(self, tokens, columns):
        self.tokens = tokens
        self.columns = columns
        self.position = 0

    def take(self, value):
        if self.position < len(self.tokens):
            kind, token = self.tokens[self.position]
            if token == value or (kind == 'word' and token.upper() == value and value in _BOOLEAN):
                self.position += 1
                return True
        return False

    @staticmethod
    def is_boolean(node):
        return node[0] in {'AND', 'OR', '=', '<>', '>=', '<=', 'IS NULL', 'IS NOT NULL'}

    def boolean_or(self):
        result = self.boolean_and()
        while self.take('OR'):
            right = self.boolean_and()
            if not self.is_boolean(result) or not self.is_boolean(right):
                raise ValueError('Unsupported frozen CHECK expression')
            result = ('OR', result, right)
        return result

    def boolean_and(self):
        result = self.comparison()
        while self.take('AND'):
            right = self.comparison()
            if not self.is_boolean(result) or not self.is_boolean(right):
                raise ValueError('Unsupported frozen CHECK expression')
            result = ('AND', result, right)
        return result

    def comparison(self):
        left = self.atom()
        for operator in ('=', '<>', '>=', '<='):
            if self.take(operator):
                right = self.atom()
                if self.is_boolean(left) or self.is_boolean(right):
                    raise ValueError('Unsupported frozen CHECK expression')
                return (operator, left, right)
        if self.take('IS'):
            negative = self.take('NOT')
            if not self.take('NULL') or self.is_boolean(left):
                raise ValueError('Unsupported frozen CHECK expression')
            return ('IS NOT NULL' if negative else 'IS NULL', left)
        return left

    def atom(self):
        if self.take('('):
            result = self.boolean_or()
            if not self.take(')'):
                raise ValueError('Unsupported frozen CHECK expression')
        else:
            if self.position >= len(self.tokens):
                raise ValueError('Unsupported frozen CHECK expression')
            kind, value = self.tokens[self.position]
            self.position += 1
            if kind == 'string':
                result = ('string', value[1:-1].replace("''", "'"))
            elif kind == 'integer':
                result = ('integer', int(value))
            elif kind == 'word' and value in self.columns:
                result = ('column', value)
            else:
                raise ValueError('Unsupported frozen CHECK expression')
        if self.take('::'):
            if self.position >= len(self.tokens) or self.tokens[self.position] != ('word', 'text'):
                raise ValueError('Unsupported frozen CHECK expression')
            self.position += 1
            if not (result[0] == 'string' or (result[0] == 'column'
                    and isinstance(self.columns[result[1]], sa.String)
                    and not isinstance(self.columns[result[1]], sa.Text))):
                raise ValueError('Unsupported frozen CHECK expression')
        return result
