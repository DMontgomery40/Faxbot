"""Installation-owned access services, prepared before the worker serves.

The caller supplies its prepared canonical ConfigurationStore. This assembly
reuses that store's exact AccessStore and loads the existing installation key
once. It does not migrate, initialize, publish runtime state or own transport.
AuthenticationWork must wrap each complete expensive authentication operation;
only the transport owner may disclose material after its confirmed return.
"""
import base64

from ..config_secrets import load_installation_key
from ..config_store import ConfigurationStore
from .admission import AuthenticationAdmission
from .authentication import AuthenticationService
from .auth_work import AuthenticationWork
from .bootstrap import BootstrapCredentials
from .credentials import CredentialCodec
from .context import ConsoleContext
from .fax_resources import FaxResources
from .mutations import AccessMutations
from .outbound import AuthorizedOutbound
from .queries import AuthorizedFaxQueries
from .policy import AccessControl
from .proofs import CredentialProofs
from .session_codec import SessionCodec
from .sessions import AccessSessions
from .types import AccessUnavailableError


def _prepare(configuration, docs_base):
    if not isinstance(configuration, ConfigurationStore):
        raise AccessUnavailableError()
    key = load_installation_key(configuration.key_path, allow_create=False)
    if type(key) is not bytes or len(key) != 44:
        raise AccessUnavailableError()
    raw_key = base64.urlsafe_b64decode(key)
    if len(raw_key) != 32:
        raise AccessUnavailableError()
    store = configuration.access_store
    credential_codec = CredentialCodec()
    session_codec = SessionCodec(installation_key=raw_key)
    bootstrap = BootstrapCredentials(configuration, installation_key=key)
    control = AccessControl(store,
        current_bootstrap_fingerprint_on=bootstrap.current_fingerprint_on)
    proofs = CredentialProofs(store, credential_codec)
    mutations = AccessMutations(store, control, credential_codec)
    sessions = AccessSessions(store, control, proofs, bootstrap, session_codec, credential_codec)
    admission = AuthenticationAdmission(store, installation_key=raw_key)
    # This constructor prepares its dummy hash after reflection has closed its
    # connection, outside every configuration/access lock and transaction.
    authentication = AuthenticationService(sessions, admission)
    fax_resources = FaxResources(control)
    outbound = AuthorizedOutbound(configuration, fax_resources)
    queries = AuthorizedFaxQueries(configuration, fax_resources)
    context = ConsoleContext(configuration, control, docs_base=docs_base)
    work = AuthenticationWork()
    return (store, credential_codec, session_codec, bootstrap, control, proofs,
            mutations, sessions, admission, authentication, fax_resources, outbound, queries, context, work)


class AccessRuntime:
    __slots__ = ('configuration', 'store', 'credential_codec', 'session_codec',
                 'bootstrap', 'control', 'proofs', 'mutations', 'sessions',
                 'admission', 'authentication', 'fax_resources', 'outbound', 'queries', 'context', 'work')

    def __init__(self, configuration: ConfigurationStore, *, docs_base='https://docs.faxbot.net/latest/'):
        services = None
        try:
            services = _prepare(configuration, docs_base)
        except Exception:
            pass
        # The preparation frame and its backend error are not retained as the
        # public failure's cause/context, including key/storage/KDF failures.
        if services is None:
            raise AccessUnavailableError()
        self.configuration = configuration
        (self.store, self.credential_codec, self.session_codec, self.bootstrap,
         self.control, self.proofs, self.mutations, self.sessions, self.admission,
         self.authentication, self.fax_resources, self.outbound, self.queries, self.context, self.work) = services

    def __repr__(self):
        return 'AccessRuntime()'
