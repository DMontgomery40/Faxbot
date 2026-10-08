"""Every route is public by design, authenticates its own capability, or declares access policy.

Routes are read from the application table; a new protected route that forgets
``require_identity``/``require_permission`` fails here with its method and path.
"""
from fastapi.routing import APIWebSocketRoute, iter_route_contexts
from starlette.routing import Mount

from app import main
from app.access.http import require_identity


# Public, or authenticated by a provider/internal capability instead of a person.
OWN_AUTHENTICATION = {
    ("GET", "/health"): "public liveness",
    ("GET", "/health/ready"): "public readiness without private state",
    ("POST", "/auth/login"): "password login",
    ("POST", "/auth/key-login"): "key-to-session login",
    ("GET", "/auth/setup"): "public: only whether a first owner is still needed, for the sign-in page",
    ("GET", "/fax/{job_id}/pdf"): "short-lived provider document token",
    ("GET", "/inbound/{inbound_id}/pdf"): "inbound:document via identity, or the fax's unexpired download token (checked in the handler)",
    ("POST", "/phaxio-callback"): "verified provider callback",
    ("POST", "/signalwire-callback"): "verified provider callback",
    ("POST", "/phaxio-inbound"): "verified provider ingest",
    ("POST", "/sinch-inbound"): "verified provider ingest",
    ("POST", "/efax-inbound"): "verified provider signal; starts a check of eFax, stores nothing",
    # One address per extra provider account, each checked with that account's own basic auth or signature.
    ("POST", "/phaxio-inbound/{key}"): "verified provider ingest for one Phaxio account (its own signature)",
    ("POST", "/sinch-inbound/{key}"): "verified provider ingest for one Sinch account (its own basic auth)",
    ("POST", "/efax-inbound/{key}"): "verified provider signal for one eFax account; stores nothing",
    ("POST", "/_internal/asterisk/inbound"): "internal shared secret",
    ("POST", "/_internal/freeswitch/outbound_result"): "internal shared secret",
    ("POST", "/_internal/hylafax/result"): "internal shared secret (the SSL Fax engine's job results)",
    ("POST", "/_internal/hylafax/started"): "the SSL Fax engine's own secret (faxes it took before a restart)",
    ("POST", "/_internal/hylafax/inbound"): "the SSL Fax engine's own secret; images in its out folder only",
    ("POST", "/_internal/hylafax/received-failed"): "the SSL Fax engine's own secret (a received call that left no fax)",
    ("POST", "/_internal/hylafax/polled"): "the SSL Fax engine's own secret (a held fax another machine collected)",
    ("POST", "/mobile/pair"): "single-use pairing code minted by a principal with tunnels:pair",
    ("WS", "/admin/terminal"): "single-use ticket from POST /admin/terminal/ticket; host:terminal rechecked while open",
    # Direct delivery partners carry no API key: each request is verified against
    # an enrolled partner's Ed25519 key, and the routes answer 404 while disabled.
    ("POST", "/direct/deliveries"): "signed partner manifest",
    ("GET", "/direct/deliveries/{message_id}"): "signed partner status request",
    ("POST", "/direct/verifications"): "signed partner code confirmation",
    ("POST", "/direct/capabilities"): "signed partner statement of what it accepts",
    ("POST", "/direct/relay/statements"): "signed partner relay statement (offer, acceptance, withdrawal, price, receipt)",
    ("GET", "/direct/relay/outcomes/{message_id}"): "signed partner request: the outcome of a fax it relayed here",
    ("POST", "/direct/introductions"): "signed partner introduction (a hint; the challenge fax still decides)",
    ("GET", "/.well-known/faxbot-direct"): "public partner card, by design; 404 while direct delivery or it is off",
    ("POST", "/direct/transfers"): "signed partner preflight: a document's manifest and pieces before its bytes",
    ("PUT", "/direct/transfers/{message_id}/pieces/{sequence}"): "signed partner request: one piece of a document",
    ("GET", "/direct/transfers/{message_id}"): "signed partner request: which pieces of a transfer are held",
    ("POST", "/direct/transfers/{message_id}/commit"): "signed partner commit of a document sent in pieces",
    ("POST", "/direct/notices"): "signed partner statement linking a notice fax to its original",
    ("POST", "/direct/notices/paired"): "signed partner statement that a notice fax was paired",
    ("POST", "/direct/calls/pages"): "signed partner question: which pages of a broken call are held",
    ("POST", "/direct/distribution/statements"): "signed partner send-once statement (offer, acceptance, withdrawal)",
    ("POST", "/direct/distributions"): "signed partner delivery: a send's first document and its list of recipients",
    ("POST", "/direct/holdings"): "signed partner question: which documents it delivered are still held",
    ("POST", "/direct/references"): "signed partner manifest for a copy of a document it delivered before",
    ("POST", "/direct/patches"): "signed partner delivery: the changes to an earlier version it delivered",
    ("GET", "/forms/partner/holdings"): "signed partner request: which registered forms this installation holds",
    ("GET", "/forms/partner/forms/{address}"): "signed partner request: one registered form by its content address",
    ("GET", "/openapi.json"): "API description",
    ("GET", "/docs"): "API description",
    ("GET", "/docs/oauth2-redirect"): "API description",
    ("GET", "/redoc"): "API description",
}
# Static files carry no authority. /admin/ui exists only with ENABLE_LOCAL_ADMIN at import.
STATIC_MOUNTS = {"/admin/ui"}
# Still on legacy guards at this revision; other slices convert them. Remove each
# entry when its route declares policy; the pending test below fails until then.
PENDING: dict = {}
# Routes kept for one release with a deprecation note in the API description.
RETIRING = {("GET", "/plugins"), ("GET", "/plugins/{plugin_id}/config"), ("PUT", "/plugins/{plugin_id}/config"),
            ("POST", "/admin/settings/persist"), ("POST", "/_internal/freeswitch/outbound_result")}
PRIVILEGED = {"host:restart", "host:terminal", "providers:install", "owner:recover"}
# Routes converted from require_admin: (permission, audited).
CONVERTED = {
    ("POST", "/admin/settings/validate"): ("providers:write", False),
    ("POST", "/admin/restart"): ("host:restart", True),
    ("GET", "/admin/health-status"): ("diagnostics:read", False),
    ("GET", "/admin/db-status"): ("diagnostics:read", False),
    ("POST", "/admin/plugins/http/install"): ("providers:install", True),
    ("POST", "/admin/plugins/http/validate"): ("providers:write", False),
    ("POST", "/admin/plugins/http/import-manifests"): ("providers:install", True),
    ("GET", "/admin/logs"): ("logs:read", False),
    ("GET", "/admin/logs/tail"): ("logs:read", False),
    ("POST", "/admin/tunnel/pair"): ("tunnels:pair", False),
    ("POST", "/admin/terminal/ticket"): ("host:terminal", True),
    ("GET", "/admin/inbound/callbacks"): ("providers:read", False),
    ("POST", "/admin/inbound/simulate"): ("providers:write", False),
    ("POST", "/inbound/{inbound_id}/fetch"): ("providers:write", True),
    ("POST", "/admin/settings/persist"): ("owner:recover", True),
}


def _calls(dependant):
    for dependency in dependant.dependencies:
        yield dependency.call
        yield from _calls(dependency)


def _routes():
    """Yield (key, calls) per method; Mounts yield ('MOUNT', path) with no calls."""
    for route in iter_route_contexts(main.app.routes):
        if isinstance(route.original_route, Mount):
            yield ("MOUNT", route.path), ()
            continue
        dependant = getattr(route, "dependant", None)
        calls = tuple(_calls(dependant)) if dependant is not None else ()
        if isinstance(route.original_route, APIWebSocketRoute):
            yield ("WS", route.path), calls
            continue
        for method in sorted(set(route.methods or ()) - {"HEAD"}):
            yield (method, route.path), calls


def _declared(calls):
    return [call.route_permission for call in calls if hasattr(call, "route_permission")]


def test_every_route_declares_access_policy_or_its_own_authentication():
    uncovered = [key for key, calls in _routes()
                 if not (key in OWN_AUTHENTICATION or key in PENDING
                         or (key[0] == "MOUNT" and key[1] in STATIC_MOUNTS)
                         or require_identity in calls)]
    assert uncovered == [], f"Routes without access policy: {uncovered}"


def test_exemptions_name_existing_routes_and_pending_ones_are_still_unconverted():
    routes = dict(_routes())
    missing = sorted((set(OWN_AUTHENTICATION) | set(PENDING)) - set(routes))
    assert missing == [], f"Exempted routes that no longer exist: {missing}"
    converted = sorted(key for key in PENDING if require_identity in routes[key])
    assert converted == [], f"Converted routes still exempted in PENDING; remove them: {converted}"
    for key in OWN_AUTHENTICATION:
        assert require_identity not in routes[key], f"{key} is exempt but also requires an identity"


def test_converted_routes_declare_their_exact_permission():
    routes = dict(_routes())
    declared = {key: [(rule.permission, rule.audit) for rule in _declared(routes[key])] for key in CONVERTED}
    assert declared == {key: [expected] for key, expected in CONVERTED.items()}
    persist = _declared(routes[("POST", "/admin/settings/persist")])[0]
    assert persist.complete_owner is True


# New routes that read installation-wide evidence: (permission, audited).
READS = {
    ("GET", "/routing/recommendations/sending"): ("settings:read", False),
    ("GET", "/routing/recommendations/receiving"): ("settings:read", False),
    ("GET", "/routing/recommendations/plans"): ("settings:read", False),
    # The dry run: what a fax would cost on each route, before sending; nothing is sent or recorded.
    ("GET", "/routing/predict"): ("settings:read", False),
    # The same with the document itself, its codings measured on its pages; nothing is kept.
    ("POST", "/routing/predict"): ("settings:read", False),
}


# Routes about one received fax: an identity, then that fax's own read check in the handler (as GET
# /inbound/{id}), never an installation-wide permission, so whoever may read the fax may read these.
FOLLOW_THE_FAX = {
    ("GET", "/inbound/{inbound_id}"),
    ("GET", "/routing/inbound/{inbound_id}/cost"),
    ("GET", "/admin/sip/negotiation/received/{inbound_id}"),
}


def test_routes_about_one_received_fax_follow_that_faxs_access():
    routes = dict(_routes())
    for key in FOLLOW_THE_FAX:
        assert require_identity in routes[key] and _declared(routes[key]) == [], key


def test_recommendation_reads_need_settings_read():
    routes = dict(_routes())
    declared = {key: [(rule.permission, rule.audit) for rule in _declared(routes[key])] for key in READS}
    assert declared == {key: [expected] for key, expected in READS.items()}


def test_privileged_permissions_are_always_audited():
    unaudited = [(key, rule.permission) for key, calls in _routes() for rule in _declared(calls)
                 if rule.permission in PRIVILEGED and not rule.audit]
    assert unaudited == []


def test_routes_removed_next_release_are_marked_deprecated_and_say_what_replaces_them():
    from app import main
    paths = main.app.openapi()["paths"]
    for method, path in RETIRING:
        operation = paths[path][method.lower()]
        assert operation.get("deprecated") is True, (method, path)
        assert "next release" in (operation.get("description") or ""), (method, path)
    marked = {(method.upper(), path) for path, operations in paths.items()
              for method, operation in operations.items() if isinstance(operation, dict) and operation.get("deprecated")}
    assert marked == RETIRING
