"""Reject display masks in plugin credentials without resolving external data."""
import re

from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, specification_with


SECRET_PLUGIN_FIELDS = frozenset({'api_key', 'api_secret', 'api_token', 'token', 'callback_token', 'password', 'secret', 'signing_key'})


class ConfigurationPluginSecretError(ValueError):
    """A credential mask is display data, never a replacement secret."""


def _check_named_secrets(value, *, secret=False):
    if isinstance(value, dict):
        for key, item in value.items():
            _check_named_secrets(item, secret=secret or key in SECRET_PLUGIN_FIELDS or key == 'credentials')
    elif isinstance(value, list):
        for item in value:
            _check_named_secrets(item, secret=secret)
    elif secret and isinstance(value, str) and re.fullmatch(r'\*+[\s\S]{0,4}', value):
        raise ConfigurationPluginSecretError('Masked credentials cannot be saved as secrets.')


def reject_masked_plugin_secrets(value, *, schema=None, validator=None):
    """Check known credential containers and applicable schema annotations.

    The caller validates the complete value first. Matching union/conditional
    branches are determined with that same validator, before mask annotations
    are considered; a permissive union cannot hide a declared secret. Each
    reference resolver contains only resources embedded in the installed schema.
    """
    _check_named_secrets(value)
    if schema is None or validator is None:
        return
    specification = specification_with(schema.get('$schema', ''), default=DRAFT202012) if isinstance(schema, dict) else DRAFT202012
    resource = Resource.from_contents(schema, default_specification=specification)
    resolver = Registry().resolver_with_root(resource)
    evaluated_by_location = {}

    def child_resolver(parent, subschema):
        return parent.in_subresource(Resource.from_contents(subschema, default_specification=specification))

    def matches(instance, subschema, parent):
        return not any(validator.descend(instance, subschema, resolver=child_resolver(parent, subschema)))

    def walk(instance, current, scope):
        if not isinstance(current, dict):
            return set()
        identity = (id(instance), id(current))
        if identity in evaluated_by_location:
            return evaluated_by_location[identity]
        evaluated = evaluated_by_location[identity] = set()
        if current.get('writeOnly') is True or current.get('format') == 'password':
            _check_named_secrets(instance, secret=True)
        for keyword in ('$ref', '$dynamicRef'):
            if keyword in current:
                resolved = scope.lookup(current[keyword])
                evaluated.update(walk(instance, resolved.contents, resolved.resolver))
        for keyword in ('allOf', 'anyOf', 'oneOf'):
            for subschema in current.get(keyword, []):
                if keyword == 'allOf' or matches(instance, subschema, scope):
                    evaluated.update(walk(instance, subschema, child_resolver(scope, subschema)))
        if 'if' in current:
            condition = matches(instance, current['if'], scope)
            if condition:
                evaluated.update(walk(instance, current['if'], child_resolver(scope, current['if'])))
            keyword = 'then' if condition else 'else'
            if keyword in current:
                evaluated.update(walk(instance, current[keyword], child_resolver(scope, current[keyword])))
        if isinstance(instance, dict):
            properties = current.get('properties', {})
            patterns = current.get('patternProperties', {})
            for key, item in instance.items():
                applicable = [properties[key]] if key in properties else []
                applicable.extend(subschema for pattern, subschema in patterns.items() if re.search(pattern, key))
                if not applicable and 'additionalProperties' in current:
                    applicable.append(current['additionalProperties'])
                for subschema in applicable:
                    walk(item, subschema, child_resolver(scope, subschema))
                    evaluated.add(key)
            for keyword in ('dependentSchemas', 'dependencies'):
                for key, subschema in current.get(keyword, {}).items():
                    if key in instance and isinstance(subschema, (dict, bool)):
                        evaluated.update(walk(instance, subschema, child_resolver(scope, subschema)))
            if 'unevaluatedProperties' in current:
                subschema = current['unevaluatedProperties']
                for key in instance.keys() - evaluated:
                    walk(instance[key], subschema, child_resolver(scope, subschema))
                    evaluated.add(key)
        elif isinstance(instance, list):
            prefix = current.get('prefixItems', [])
            items = current.get('items', {})
            if isinstance(items, list):
                prefix, items = items, current.get('additionalItems', {})
            for index, item in enumerate(instance):
                if index < len(prefix) or 'items' in current:
                    subschema = prefix[index] if index < len(prefix) else items
                    walk(item, subschema, child_resolver(scope, subschema))
                    evaluated.add(index)
                if 'contains' in current and matches(item, current['contains'], scope):
                    walk(item, current['contains'], child_resolver(scope, current['contains']))
                    evaluated.add(index)
            if 'unevaluatedItems' in current:
                subschema = current['unevaluatedItems']
                for index in set(range(len(instance))) - evaluated:
                    walk(instance[index], subschema, child_resolver(scope, subschema))
                    evaluated.add(index)
        return evaluated

    walk(value, schema, resolver)
