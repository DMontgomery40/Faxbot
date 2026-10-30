import os
import sys
import pytest

# Ensure project root is on sys.path for `import api.*`
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Ensure test mode flag for Ghostscript-less CI/unit tests
os.environ.setdefault("FAXBOT_TEST_MODE", "true")


@pytest.fixture(autouse=True)
def _restore_installation_database():
    """The process's installation database binding, back after every test.

    An app lifespan whose DATABASE_URL differs moves ``db.engine`` and ``db.SessionLocal`` to that database
    (``db.init_db``) and never moves them back. A test on a scoped PostgreSQL schema then left every later test
    reading a schema it dropped, so a trunk call later in the run failed before its Originate was written. The
    module is loaded both as ``app.db`` and as ``api.app.db``; each loaded copy is restored.
    """
    saved = {name: sys.modules[name].engine for name in ('app.db', 'api.app.db') if name in sys.modules}
    yield
    for name, engine in saved.items():
        module = sys.modules.get(name)
        if module is None or module.engine is engine:
            continue
        moved = module.engine
        module.SessionLocal.configure(bind=engine)
        module.engine = engine
        moved.dispose()


@pytest.fixture
def isolated_installation(monkeypatch, tmp_path):
    """Opt-in real installation boundary for legacy lifespan/HTTP fixtures.

    Canonical configuration belongs to a database and its key, not to the
    current process environment. Each test gets a new installation; sequential
    lifespans within that test deliberately retain the same canonical state.
    """
    from app.config_values import ConfigurationValues
    for name in ConfigurationValues.environment_keys():
        monkeypatch.delenv(name, raising=False)
    environment = {
        'DATABASE_URL': 'sqlite:///' + str(tmp_path / 'installation.db'),
        'FAX_DATA_DIR': str(tmp_path / 'faxdata'),
        'FAXBOT_INSTALLATION_KEY_PATH': str(tmp_path / 'installation.key'),
        'FAXBOT_CONFIG_PATH': str(tmp_path / 'absent-legacy.json'),
        'FAXBOT_PROVIDERS_DIR': str(tmp_path / 'providers'),
        'FAX_DISABLED': 'true',
        # A new installation has no fax provider; these legacy fixtures exercise a Phaxio one.
        'FAX_BACKEND': 'phaxio',
        'REQUIRE_API_KEY': 'false',
        'ENABLE_PERSISTED_SETTINGS': 'false',
        'ENABLE_MCP_SSE': 'false',
        'ENABLE_MCP_HTTP': 'false',
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return environment


# Test files too slow for the pull-request gate (.github/workflows/ci.yml). In the eight-group run of
# 873d5a20f their SQLite cases averaged more than 0.5 s each, or one took more than 10 s; most build an
# installation database for every case. Every test in these files, every PostgreSQL variant and every
# Docker proof (``native``) is marked ``heavy``. The gate runs ``-m 'not heavy'``; the full suite
# (.github/workflows/full-suite.yml) runs everything on release tags and by hand. A new test file runs
# in the gate until it is listed here. The marker only selects: without ``-m`` nothing is skipped.
HEAVY_TEST_FILES = frozenset({
    'test_access_admission.py',
    'test_access_authentication.py',
    'test_access_bootstrap.py',
    'test_access_capabilities.py',
    'test_access_configuration_access.py',
    'test_access_configuration_writes.py',
    'test_access_context.py',
    'test_access_fax_resources.py',
    'test_access_http_transport.py',
    'test_access_management_http.py',
    'test_access_mutation_races.py',
    'test_access_mutations.py',
    'test_access_outbound.py',
    'test_access_policy.py',
    'test_access_projection.py',
    'test_access_proofs.py',
    'test_access_queries.py',
    'test_access_read.py',
    'test_access_runtime.py',
    'test_access_schema.py',
    'test_access_sessions.py',
    'test_access_store.py',
    'test_accounts_schema.py',
    'test_admin_hybrid.py',
    'test_advice.py',
    'test_advice_cli.py',
    'test_after_answer.py',
    'test_after_answer_schema.py',
    'test_analog_line.py',
    'test_analysis.py',
    'test_analysis_schema.py',
    'test_answer_cap.py',
    'test_api.py',
    'test_api_keys.py',
    'test_api_scopes.py',
    'test_authentication_schema.py',
    'test_batching.py',
    'test_batching_http.py',
    'test_batching_index_page.py',
    'test_batching_schema.py',
    'test_body_reuse.py',
    'test_call_hours.py',
    'test_caller_numbers.py',
    'test_capabilities.py',
    'test_capacity.py',
    'test_capacity_schema.py',
    'test_carrier_charges.py',
    'test_case_checklists.py',
    'test_case_ledger_schema.py',
    'test_case_ledger_store.py',
    'test_case_packets_schema.py',
    'test_cases.py',
    'test_certainty.py',
    'test_certainty_http.py',
    'test_certainty_schema.py',
    'test_charges_cli.py',
    'test_charges_schema.py',
    'test_cli.py',
    'test_cli_admin.py',
    'test_cli_cases.py',
    'test_cli_certainty.py',
    'test_cli_continuation.py',
    'test_cli_expected.py',
    'test_cli_rules.py',
    'test_cli_work.py',
    'test_client_key_compat.py',
    'test_closures.py',
    'test_codec.py',
    'test_codec_candidate_pricing.py',
    'test_codec_capacity.py',
    'test_codec_decoder.py',
    'test_codec_decoder_schema.py',
    'test_codec_decoder_setting.py',
    'test_codec_delivery.py',
    'test_codec_receive_recovery.py',
    'test_codec_retention.py',
    'test_codec_schema.py',
    'test_combinations_faults.py',
    'test_combinations_identity.py',
    'test_combinations_inbound.py',
    'test_combinations_pages.py',
    'test_combinations_pricing.py',
    'test_combinations_queue.py',
    'test_combinations_sslfax.py',
    'test_combinations_submit.py',
    'test_combinations_together.py',
    'test_config_activation.py',
    'test_config_env_secrets.py',
    'test_config_paths.py',
    'test_config_store.py',
    'test_configuration_audit_lifecycle.py',
    'test_configuration_runtime.py',
    'test_configuration_store.py',
    'test_continuation.py',
    'test_continuation_schema.py',
    'test_copier_extensions.py',
    'test_countries_schema.py',
    'test_court_profile.py',
    'test_database_busy.py',
    'test_delivery_schema.py',
    'test_dense_pages.py',
    'test_destination_capture_pricing.py',
    'test_destination_plan_pricing.py',
    'test_destination_schedule_schema.py',
    'test_dialing_guard.py',
    'test_dialing_schema.py',
    'test_digital_cli.py',
    'test_digital_direct.py',
    'test_digital_fhir.py',
    'test_digital_fhir_patient.py',
    'test_digital_http.py',
    'test_digital_routing.py',
    'test_digital_schema.py',
    'test_direct_call_repair.py',
    'test_direct_delivery.py',
    'test_direct_notice.py',
    'test_direct_pins.py',
    'test_direct_send_once.py',
    'test_direct_store.py',
    'test_direct_transfer.py',
    'test_discovery.py',
    'test_discovery_cli.py',
    'test_discovery_partners.py',
    'test_discovery_schema.py',
    'test_documents.py',
    'test_documo_captured_delivery.py',
    'test_efax.py',
    'test_efax_prices.py',
    'test_efax_receiving.py',
    'test_encoder_tuning_schema.py',
    'test_engine_extras_schema.py',
    'test_engine_frames.py',
    'test_engine_learning.py',
    'test_engine_learning_schema.py',
    'test_engine_login.py',
    'test_engine_subaddress.py',
    'test_expected_faxes.py',
    'test_expected_faxes_http.py',
    'test_expected_faxes_schema.py',
    'test_fact_advice.py',
    'test_fact_advice_schema.py',
    'test_faithful_choice.py',
    'test_fax_engine_schema.py',
    'test_fax_friendly.py',
    'test_fax_patient_http.py',
    'test_fax_screens.py',
    'test_fax_server_renewal_schema.py',
    'test_fax_servers.py',
    'test_forms_cli.py',
    'test_forms_exchange.py',
    'test_forms_registry.py',
    'test_forms_schema.py',
    'test_freeswitch.py',
    'test_header_notice.py',
    'test_header_notice_schema.py',
    'test_history_schema.py',
    'test_humblefax_receiving.py',
    'test_hylafax_records.py',
    'test_hylafax_results.py',
    'test_hylafax_screens.py',
    'test_hylafax_scripts.py',
    'test_inbound_access.py',
    'test_inbound_acquisition.py',
    'test_inbound_handover.py',
    'test_inbound_internal.py',
    'test_inbound_provider_ingest.py',
    'test_inbound_schema.py',
    'test_inbound_sources_schema.py',
    'test_intake.py',
    'test_intake_http.py',
    'test_intake_sources.py',
    'test_intake_sources_schema.py',
    'test_invoices.py',
    'test_invoices_schema.py',
    'test_jbig_coding.py',
    'test_joint_hooks.py',
    'test_joint_optimizer.py',
    'test_joint_slots.py',
    'test_line_advice.py',
    'test_line_inventory_http.py',
    'test_line_inventory_schema.py',
    'test_local_delivery_schema.py',
    'test_long_faxes.py',
    'test_measured_codec_schema.py',
    'test_mobile_compat.py',
    'test_mobile_pairing.py',
    'test_notice_repair_schema.py',
    'test_number_advice.py',
    'test_number_advice_cli.py',
    'test_number_advice_http.py',
    'test_number_advice_schema.py',
    'test_origin_classes.py',
    'test_origin_rates.py',
    'test_outbound_callbacks.py',
    'test_outbound_idempotency.py',
    'test_outbound_lease_clock.py',
    'test_outbound_now.py',
    'test_outbound_polling.py',
    'test_outbound_reconciliation.py',
    'test_outbound_schema.py',
    'test_outbound_store.py',
    'test_outbound_summary.py',
    'test_outbound_transport.py',
    'test_outbound_wake.py',
    'test_outbound_worker.py',
    'test_partly_sent.py',
    'test_partner_admission.py',
    'test_partner_layouts.py',
    'test_partner_relay.py',
    'test_partner_relay_schema.py',
    'test_partner_relay_terms.py',
    'test_peer_fax.py',
    'test_peer_fax_schema.py',
    'test_person_answered.py',
    'test_phaxio.py',
    'test_plan_allocation.py',
    'test_plan_budget.py',
    'test_plan_check.py',
    'test_polled_transmit_schema.py',
    'test_polling.py',
    'test_portfolio_surface.py',
    'test_pots_quote.py',
    'test_power.py',
    'test_provider_accounts.py',
    'test_provider_charges.py',
    'test_receive_readiness.py',
    'test_receiving_accounts.py',
    'test_receiving_rules.py',
    'test_receiving_rules_http.py',
    'test_receiving_savings.py',
    'test_records_schema.py',
    'test_release_upgrade.py',
    'test_renewal_http.py',
    'test_reply_number.py',
    'test_retired_permissions_schema.py',
    'test_route_families.py',
    'test_route_families_http.py',
    'test_route_families_schema.py',
    'test_route_policy.py',
    'test_route_selections_schema.py',
    'test_routing_delivered.py',
    'test_routing_http.py',
    'test_routing_learning_schema.py',
    'test_routing_local.py',
    'test_routing_multiprovider.py',
    'test_routing_predict.py',
    'test_routing_rules_schema.py',
    'test_routing_store.py',
    'test_rules_delivery.py',
    'test_rules_delivery_http.py',
    'test_rules_delivery_schema.py',
    'test_rules_http.py',
    'test_rules_store.py',
    'test_runtime.py',
    'test_savings_mechanisms.py',
    'test_schedule.py',
    'test_schema.py',
    'test_screening.py',
    'test_screening_schema.py',
    'test_send_once_schema.py',
    'test_sender_pins.py',
    'test_sent_since.py',
    'test_setup_plan_cli.py',
    'test_setup_plan_http.py',
    'test_setup_plan_store.py',
    'test_setup_plans_schema.py',
    'test_shading_method_schema.py',
    'test_shared_manifest_schema.py',
    'test_sinch_route.py',
    'test_sip_calls.py',
    'test_sip_fax_mode.py',
    'test_sip_http.py',
    'test_sip_network.py',
    'test_station_check_e2e.py',
    'test_station_check_schema.py',
    'test_telnyx_fax_http.py',
    'test_telnyx_names.py',
    'test_telnyx_t38.py',
    'test_terminal_schema.py',
    'test_terminal_websocket.py',
    'test_test_lines.py',
    'test_test_lines_schema.py',
    'test_tollfree.py',
    'test_tollfree_dialing.py',
    'test_trunk_capacity.py',
    'test_trunks_sites_schema.py',
    'test_work_duplicates.py',
    'test_work_http.py',
    'test_work_schema.py',
    'test_work_store.py',
})


def _postgresql_variant(item):
    callspec = getattr(item, 'callspec', None)
    return callspec is not None and any(part in ('postgres', 'postgresql') for part in callspec.id.split('-'))


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Mark what the pull-request gate leaves to the full suite, before ``-m`` deselects by marker."""
    for item in items:
        if (item.path.name in HEAVY_TEST_FILES or item.get_closest_marker('native') is not None
                or _postgresql_variant(item)):
            item.add_marker(pytest.mark.heavy)


def pytest_configure(config):
    config.addinivalue_line('markers', 'native: needs Docker and the Faxbot Asterisk image; runs only with FAXBOT_NATIVE_PROOF=1')
    config.addinivalue_line('markers', 'heavy: left to the full suite; the pull-request gate runs -m "not heavy" (see HEAVY_TEST_FILES)')
