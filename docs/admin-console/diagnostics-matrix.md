# Diagnostics Matrix

Use the active provider and revision shown in [Diagnostics](diagnostics.md) when interpreting a result. The default provider may differ from active outbound or inbound selection. Apply configuration changes through the canonical Settings editor and check whether they became active or remain pending.

| Result | Follow-up |
| --- | --- |
| Active outbound configuration fails | Open Settings and inspect the active provider's required fields. For a custom manifest, inspect its configured adapter in Plugins. Credentials for an unused default provider do not repair the active adapter. |
| Required Asterisk AMI connection fails | Verify the active AMI host, port, username, password and Asterisk service. A connection check is not fax-delivery proof. |
| Native Asterisk password is empty or default | Set a non-default AMI password in both the Asterisk service and the desired provider settings; inspect activation status. |
| Native Asterisk inbound secret is absent | Select **Apply to Asterisk** on the trunk screen (or restart Faxbot); Faxbot creates the secret and writes it for Asterisk. |
| Ghostscript fails | Install `gs` in the API runtime and rerun Diagnostics. Document processing requires it. |
| Fax data directory is absent or unwritable | Check the configured installation path, mount and service-user permissions. Moving installation storage requires the maintenance workflow. |
| Temporary directory is unwritable | Check runtime temporary-directory permissions and available storage. |
| Database connection fails | Check the configured database service or SQLite mount and service-user access. Do not replace the installation database to clear the error. |
| Required inbound storage fails | Review Storage settings and the active receiving provider. The optional S3 access check needs the deployment's `ENABLE_S3_DIAGNOSTICS=true` flag; a missing probe is not proof of bucket access. |
| Audit, HTTPS enforcement or rate limiting warning | Review the relevant desired settings and the installation's network configuration. These flags alone do not establish end-to-end security. |
| Plugin or trait metadata warning | Inspect the identified installed manifest and schema. File inventory and the captured active provider revision are distinct. |
| Desired revision is pending | Arrange a full installation stop/restart, then confirm active and desired identity. Restart API exits only one process. |
| Receiving or remote plugin installation is disabled | Informational feature state; enable it only when that capability is intended and configured. |

## Provider validation

Local readiness does not test callback reachability or remote delivery. Use the relevant setup guide and a destination you control:

- [Phaxio](../setup/phaxio.md)
- [Sinch](../setup/sinch.md)
- [Documo](../setup/documo.md)
- [SignalWire](../setup/signalwire.md)
- [SIP/Asterisk](../setup/sip-asterisk.md)
- [FreeSWITCH](../setup/freeswitch.md)

Open Send from Diagnostics to use the retained document, destination and request-intent workflow. Acceptance is not delivery. Verify the final job/provider result and the received document.
