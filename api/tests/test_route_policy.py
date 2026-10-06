"""Installation permissions on administrative routes, over the real HTTPS stack."""
from datetime import timedelta
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main
from app.access import admission
from app.access.catalog import BUILTIN_ROLE_PERMISSIONS, GLOBAL_PERMISSIONS
from app.access.http import utcnow
from app.access.mutation_types import (AssignmentValues, CustomRoleValues, IntegrationValues, KeyValues,
                                       MutationDeniedError, MutationReason, PrincipalSubject, VersionedEntity)
from app.access.route_policy import authorize, require_permission
from app.access.types import ResourceRef, ScopedPermission


BOOTSTRAP = "synthetic-route-policy-bootstrap"
ADMIN = {"X-API-Key": BOOTSTRAP}
MANIFEST = {"id": "synthetic-policy.v1", "name": "Synthetic policy provider",
            "allowed_domains": ["synthetic.invalid"],
            "actions": {"send_fax": {"url": "https://synthetic.invalid/send"},
                        "get_status": {"url": "https://synthetic.invalid/status"}}}

# Every route this policy converted, with a body that would succeed if allowed.
CONVERTED = [
    ("POST", "/admin/settings/validate", {"backend": "sinch"}),
    ("POST", "/admin/restart", None),
    ("GET", "/admin/health-status", None),
    ("GET", "/admin/db-status", None),
    ("POST", "/admin/plugins/http/install", {"manifest": MANIFEST}),
    ("POST", "/admin/plugins/http/validate", {"manifest": MANIFEST}),
    ("POST", "/admin/plugins/http/import-manifests", {"items": [MANIFEST]}),
    ("GET", "/admin/logs", None),
    ("GET", "/admin/logs/tail", None),
    ("GET", "/admin/inbound/callbacks", None),
    ("POST", "/admin/inbound/simulate", {}),
    ("POST", "/admin/settings/persist", {}),
]


def _client(monkeypatch, tmp_path, **overrides):
    environment = {
        "REQUIRE_API_KEY": "true", "API_KEY": BOOTSTRAP,
        "PUBLIC_API_URL": "https://testserver", "FAX_BACKEND": "phaxio",
        "FEATURE_V3_PLUGINS": "true", "MAX_REQUESTS_PER_MINUTE": "0",
        "PERSISTED_ENV_PATH": str(tmp_path / "recovery" / "faxbot.env"),
        **overrides,
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    monkeypatch.delenv("ENABLE_ADMIN_EXEC", raising=False)
    monkeypatch.delenv("ENABLE_LOCAL_ADMIN", raising=False)
    # No default X-API-Key: some requests must carry none, or a session cookie.
    return TestClient(main.app, base_url="https://testserver", headers={"Origin": "https://testserver"})


@pytest.fixture
def client(isolated_installation, monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        yield client


def _call(client, method, path, body=None, headers=None):
    return client.request(method, path, json=body, headers=headers or {})


def _scoped_key(client, scopes):
    response = client.post("/admin/api-keys", headers=ADMIN, json={"name": "synthetic", "scopes": scopes})
    assert response.status_code == 200, response.text
    return {"X-API-Key": response.json()["token"]}


def _role_key(client, *, role_id=None, permissions=frozenset()):
    """A key for a new integration holding one role at the installation."""
    runtime = client.app.state.access_runtime
    tables = runtime.store.tables
    bootstrap = runtime.authentication.header_key(BOOTSTRAP)

    def version(table, identity=None):
        with runtime.store.engine.connect() as connection:
            if table == "access_state":
                return connection.execute(sa.select(tables[table].c.policy_version)).scalar_one()
            return connection.execute(sa.select(tables[table].c.version)
                                      .where(tables[table].c.id == identity)).scalar_one()

    def call(method, *args):
        return getattr(runtime.mutations, method)(bootstrap, *args,
                                                  expected_policy_version=version("access_state"), now=utcnow())

    if role_id is None:
        role = call("create_custom_role", CustomRoleValues("Synthetic role", "synthetic", True, permissions)).target
    else:
        role, permissions = VersionedEntity(role_id, version("access_roles", role_id)), BUILTIN_ROLE_PERMISSIONS[role_id]
    principal = call("create_integration", IntegrationValues("Synthetic integration", True)).target
    call("create_assignment", AssignmentValues(PrincipalSubject(principal), role, ResourceRef("installation")))
    prepared = runtime.credential_codec.prepare_new_key()
    ceiling = tuple(ScopedPermission(permission, ResourceRef("installation"))
                    for permission in sorted(permissions & GLOBAL_PERMISSIONS))
    call("issue_key", KeyValues(VersionedEntity(principal.id, version("access_principals", principal.id)),
                                "synthetic", None, None, ceiling), prepared)
    return {"X-API-Key": prepared._token_for_committed_adapter()}, principal.id


def _audits(client, operation):
    runtime = client.app.state.access_runtime
    audit = runtime.store.tables["access_audit"]
    with runtime.store.engine.connect() as connection:
        rows = connection.execute(sa.select(audit).where(audit.c.operation == operation)
                                  .order_by(audit.c.created_at)).mappings().all()
    return [(row["outcome"], row["actor_principal_id"], json.loads(row["details"])) for row in rows]


def test_missing_or_empty_key_is_unauthenticated(client):
    for method, path, body in CONVERTED:
        assert _call(client, method, path, body).status_code == 401, (method, path)
        assert _call(client, method, path, body, {"X-API-Key": ""}).status_code == 401, (method, path)


def test_legacy_anonymous_mode_no_longer_opens_converted_routes(isolated_installation, monkeypatch):
    # REQUIRE_API_KEY=false with no API_KEY used to mean "anonymous dev mode".
    assert isolated_installation["REQUIRE_API_KEY"] == "false"
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    with TestClient(main.app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        for method, path, body in CONVERTED:
            assert _call(client, method, path, body).status_code == 401, (method, path)


def test_fax_send_and_key_manager_keys_are_denied_every_converted_route(client, tmp_path, monkeypatch):
    # This sweep sends more key requests than the authentication bursts allow.
    monkeypatch.setitem(admission._BUDGETS, "key_request", admission._Budget("key-request", 100, timedelta(milliseconds=1)))
    monkeypatch.setattr(admission, "_GLOBAL", admission._Budget("global", 100, timedelta(milliseconds=1)))
    # Legacy require_admin let any keys:manage key run host actions and read logs.
    for headers in (_scoped_key(client, ["fax:send"]), _scoped_key(client, ["keys:manage"])):
        for method, path, body in CONVERTED:
            response = _call(client, method, path, body, headers)
            assert response.status_code == 403, (method, path, response.text)
            assert response.json() == {"detail": "This operation is not permitted."}
    assert not (tmp_path / "recovery").exists()


def test_bootstrap_key_reaches_each_permission_family(client, tmp_path):
    validate = _call(client, "POST", "/admin/settings/validate", {"backend": "sinch"}, ADMIN)
    assert validate.status_code == 200 and validate.json()["checks"]["auth"] is False
    assert _call(client, "GET", "/admin/inbound/callbacks", None, ADMIN).status_code == 200
    assert _call(client, "GET", "/admin/health-status", None, ADMIN).json()["require_auth"] is True
    assert _call(client, "GET", "/admin/logs", None, ADMIN).status_code == 200

    installed = _call(client, "POST", "/admin/plugins/http/install", {"manifest": MANIFEST}, ADMIN)
    assert installed.status_code == 200, installed.text
    assert [(outcome, actor) for outcome, actor, _ in _audits(client, "providers.install")] == [("allowed", "bootstrap")]

    # The restart gate still applies after the permission check, which is audited.
    restart = _call(client, "POST", "/admin/restart", None, ADMIN)
    assert restart.status_code == 403 and restart.json()["detail"] == "Restart not allowed"
    assert _audits(client, "host.restart") == [("allowed", "bootstrap", {"request": "POST /admin/restart"})]

    persisted = _call(client, "POST", "/admin/settings/persist", {}, ADMIN)
    assert persisted.status_code == 200, persisted.text
    assert Path(persisted.json()["path"]) == tmp_path / "recovery" / "faxbot.env"
    assert f"API_KEY={BOOTSTRAP}" in (tmp_path / "recovery" / "faxbot.env").read_text()
    assert _audits(client, "owner.recover") == [("allowed", "bootstrap", {"request": "POST /admin/settings/persist"})]


def test_db_status_counts_only_what_the_caller_could_list(client):
    _scoped_key(client, ["fax:read"])
    sent = client.post("/fax", headers=ADMIN, data={"to": "+15551230001"},
                       files={"file": ("synthetic.txt", b"synthetic", "text/plain")})
    assert sent.status_code == 202, sent.text
    assert _call(client, "GET", "/admin/db-status", None, ADMIN).json()["counts"] == {
        "fax_jobs": 1, "api_keys": 1, "inbound_fax": 0}
    operator, _ = _role_key(client, role_id="role_host_operator")
    assert _call(client, "GET", "/admin/db-status", None, operator).json()["counts"] == {
        "fax_jobs": 0, "api_keys": None, "inbound_fax": 0}


def test_administrator_reads_operations_but_cannot_use_host_or_recovery(client):
    headers, principal = _role_key(client, role_id="role_administrator")
    assert _call(client, "GET", "/admin/logs", None, headers).status_code == 200
    assert _call(client, "GET", "/admin/health-status", None, headers).status_code == 200
    assert _call(client, "POST", "/admin/plugins/http/install", {"manifest": MANIFEST}, headers).status_code == 200
    for method, path in (("POST", "/admin/restart"), ("POST", "/admin/terminal/ticket"), ("POST", "/admin/settings/persist")):
        assert _call(client, method, path, {} if method == "POST" else None, headers).status_code == 403
    assert _audits(client, "host.restart") == [
        ("denied", principal, {"request": "POST /admin/restart", "reason": "forbidden"})]
    assert _audits(client, "host.terminal") == [
        ("denied", principal, {"request": "POST /admin/terminal/ticket", "reason": "forbidden"})]


def test_recovery_export_requires_complete_owner_authority(client, tmp_path):
    headers, principal = _role_key(client, permissions=frozenset({"owner:recover"}))
    response = _call(client, "POST", "/admin/settings/persist", {}, headers)
    assert response.status_code == 403
    assert _audits(client, "owner.recover") == [
        ("denied", principal, {"request": "POST /admin/settings/persist", "reason": "owner_required"})]
    assert not (tmp_path / "recovery").exists()


def test_manifest_install_keeps_the_v3_plugins_gate(isolated_installation, monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, FEATURE_V3_PLUGINS="false") as client:
        assert _call(client, "POST", "/admin/plugins/http/install", {"manifest": MANIFEST}, ADMIN).status_code == 404
        assert _call(client, "POST", "/admin/plugins/http/import-manifests",
                     {"items": [MANIFEST]}, ADMIN).status_code == 404
        assert not (tmp_path / "providers" / "synthetic-policy.v1").exists()


def test_browser_session_needs_csrf_for_converted_mutations(client):
    login = client.post("/auth/key-login", json={"api_key": BOOTSTRAP})
    assert login.status_code == 200, login.text
    body = {"backend": "sinch"}
    assert client.get("/admin/logs").status_code == 200
    assert client.post("/admin/settings/validate", json=body).status_code == 403
    csrf = client.get("/auth/me").json()["csrf_token"]
    assert client.post("/admin/settings/validate", json=body, headers={"X-CSRF-Token": csrf}).status_code == 200


def test_fax_send_is_checked_at_the_personal_container(client):
    runtime = client.app.state.access_runtime
    sender = runtime.authentication.header_key(_scoped_key(client, ["fax:send"])["X-API-Key"])
    manager = runtime.authentication.header_key(_scoped_key(client, ["keys:manage"])["X-API-Key"])
    authorize(runtime, sender, "fax:send")
    authorize(runtime, runtime.authentication.header_key(BOOTSTRAP), "fax:send")
    with pytest.raises(MutationDeniedError) as denied:
        authorize(runtime, manager, "fax:send")
    assert denied.value.reason is MutationReason.FORBIDDEN


@pytest.mark.parametrize("permission,options", [
    ("fax:send", {}), ("fax:read", {}), ("inbound:list", {}), ("not:a-permission", {}),
    ("logs:read", {"resource": "personal"}), ("fax:send", {"resource": "personal", "audit": True}),
])
def test_require_permission_refuses_resource_scoped_or_unknown_permissions(permission, options):
    with pytest.raises(ValueError):
        require_permission(permission, **options)
