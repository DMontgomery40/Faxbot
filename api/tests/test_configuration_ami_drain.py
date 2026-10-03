"""Internal profile/trait drain test; no listener or provider requests."""
from datetime import datetime
import json
import pytest


def test_captured_manifest_ami_requirement_protects_unresolved_work(tmp_path):
    from app.config_runtime import ConfigurationRuntime
    from app.config_activation import ConfigurationActivationError
    providers = tmp_path / 'providers'
    manifest = providers / 'ami-http-bridge' / 'manifest.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'id': 'ami-http-bridge', 'traits': {'requires_ami': True},
        'actions': {'send_fax': {'url': 'https://synthetic-provider.invalid/fax', 'method': 'POST'}}}))
    environment = {'DATABASE_URL': 'sqlite:///' + str(tmp_path / 'installation.db'),
        'FAX_DATA_DIR': str(tmp_path / 'data'), 'FAX_OUTBOUND_BACKEND': 'ami-http-bridge',
        'FEATURE_V3_PLUGINS': 'true', 'FAXBOT_PROVIDERS_DIR': str(providers),
        'FAXBOT_CONFIG_PATH': str(tmp_path / 'missing.json')}
    first = ConfigurationRuntime(environment).prepare()
    try:
        profile = first.manager.store.read_profile(first.snapshot.active.profile_id('outbound'))
        assert profile.configuration.traits['requires_ami'] is True
        now = datetime.utcnow()
        first.manager.store.accept_outbound(first.snapshot.active, {
            'id': 'synthetic-unresolved-manifest', 'to_number': '+15550000000',
            'file_name': 'synthetic.pdf', 'tiff_path': '', 'status': 'queued',
            'created_at': now, 'updated_at': now})
        staged = first.manager.patch(first.snapshot, {'ami_password': 'synthetic-new-ami-password'}, actor='review')
        assert staged.pending is not None
    finally:
        first.close()
    second = ConfigurationRuntime(environment)
    try:
        with pytest.raises(ConfigurationActivationError):
            second.prepare()
    finally:
        second.close()
