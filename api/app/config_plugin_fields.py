"""Retained plugin field names mapped to the canonical configuration model."""

PLUGIN_FIELDS = {
    'phaxio': {'api_key': 'phaxio_api_key', 'api_secret': 'phaxio_api_secret',
               'callback_url': 'phaxio_status_callback_url', 'verify_signature': 'phaxio_verify_signature',
               'inbound_verify_signature': 'phaxio_inbound_verify_signature'},
    'sinch': {'project_id': 'sinch_project_id', 'api_key': 'sinch_api_key',
              'api_secret': 'sinch_api_secret', 'base_url': 'sinch_base_url',
              'inbound_basic_user': 'sinch_inbound_basic_user', 'inbound_basic_pass': 'sinch_inbound_basic_pass',
              'inbound_hmac_secret': 'sinch_inbound_hmac_secret', 'inbound_verify_signature': 'sinch_inbound_verify_signature'},
    'signalwire': {'space_url': 'signalwire_space_url', 'project_id': 'signalwire_project_id',
                   'api_token': 'signalwire_api_token', 'fax_from_e164': 'signalwire_fax_from_e164',
                   'sms_from_e164': 'signalwire_sms_from_e164', 'callback_url': 'signalwire_status_callback_url',
                   'webhook_signing_key': 'signalwire_webhook_signing_key',
                   'status_poll_seconds': 'signalwire_status_poll_seconds'},
    'documo': {'api_key': 'documo_api_key', 'base_url': 'documo_base_url', 'sandbox': 'documo_use_sandbox'},
    'sip': {'ami_host': 'ami_host', 'ami_port': 'ami_port', 'ami_username': 'ami_username',
            'ami_password': 'ami_password', 'inbound_secret': 'asterisk_inbound_secret'},
    'freeswitch': {'esl_host': 'fs_esl_host', 'esl_port': 'fs_esl_port', 'esl_password': 'fs_esl_password',
                   'gateway_name': 'fs_gateway_name', 'caller_id_number': 'fs_caller_id_number', 't38_enable': 'fs_t38_enable'},
    's3': {'bucket': 's3_bucket', 'prefix': 's3_prefix', 'region': 's3_region',
           'endpoint_url': 's3_endpoint_url', 'kms_key_id': 's3_kms_key_id'},
    'local': {},
}
