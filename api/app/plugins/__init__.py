"""HTTP manifest providers.

``http_provider`` sends faxes through another provider's HTTP API, as described
by a JSON manifest. ``provider_catalog`` lists the manifests found under
``FAXBOT_PROVIDERS_DIR``. ``main`` reads manifests with this module when they
are installed or validated, and ``provider_execution`` sends through them.
Installing a manifest through the API still needs ``FEATURE_V3_PLUGINS``.
"""
