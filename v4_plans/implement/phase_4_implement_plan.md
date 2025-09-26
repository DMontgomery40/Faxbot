# Phase 4: Enterprise Integration & Advanced Plugin Ecosystem

**Branch**: `auto-tunnel` (CRITICAL - all v4 work stays here)
**Status**: Comprehensive implementation plan including all existing infrastructure
**Timeline**: 7-8 weeks

1

```

### 3. Admin Console Marketplace UI (HIPAA-aware search filters)

**Create `api/admin_ui/src/components/PluginMarketplace.tsx`**:
```typescript
import React, { useState, useEffect } from 'react';
import {
  Grid, Card, CardContent, Typography, Button, Chip, Box,
  TextField, InputAdornment, Dialog, DialogTitle, DialogContent,
  Alert, Rating, Avatar, Divider, Tab, Tabs
} from '@mui/material';
import { Search as SearchIcon, Security as SecurityIcon } from '@mui/icons-material';

interface Plugin {
  id: string;
  name: string;
  version: string;
  description: string;
  author: string;
  categories: string[];
  hipaa_compliant: boolean;
  signature_verified: boolean;
  downloads: number;
  rating: number;
  icon?: string;
  screenshots?: string[];
}

export default function PluginMarketplace({ client, docsBase }: PluginMarketplaceProps) {
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedCategory, setSelectedCategory] = useState('all');
  const [selectedPlugin, setSelectedPlugin] = useState<Plugin | null>(null);
  const [installing, setInstalling] = useState<string | null>(null);

  const categories = [
    { id: 'all', label: 'All Plugins' },
    { id: 'transport', label: 'Fax Transport' },
    { id: 'storage', label: 'Storage' },
    { id: 'identity', label: 'Authentication' },
    { id: 'messaging', label: 'Messaging' },
    { id: 'integration', label: 'Enterprise Integration' }
  ];

  useEffect(() => {
    loadPlugins();
  }, [searchQuery, selectedCategory]);

  const loadPlugins = async () => {
    try {
      const response = await client.get('/admin/marketplace/plugins', {
        params: {
          search: searchQuery,
          category: selectedCategory !== 'all' ? selectedCategory : undefined
        }
      });
      setPlugins(response.data.plugins);
    } catch (error) {
      console.error('Failed to load plugins:', error);
    }
  };

  const installPlugin = async (pluginId: string) => {
    setInstalling(pluginId);
    try {
      // Server enforces flags: admin.marketplace.enabled + admin.marketplace.remote_install_enabled
      await client.post(`/admin/marketplace/install`, { plugin_id: pluginId });
      // Refresh installed plugins list
      await loadPlugins();
    } catch (error) {
      console.error('Installation failed:', error);
    } finally {
      setInstalling(null);
    }
  };

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 3 }}>Plugin Marketplace</Typography>

      {/* Search and Filter Bar */}
      <Box sx={{ mb: 3, display: 'flex', gap: 2, alignItems: 'center' }}>
        <TextField
          placeholder="Search plugins..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchIcon />
              </InputAdornment>
            ),
          }}
          sx={{ flexGrow: 1 }}
        />
        <Tabs
          value={selectedCategory}
          onChange={(_, value) => setSelectedCategory(value)}
          variant="scrollable"
        >
          {categories.map(cat => (
            <Tab key={cat.id} value={cat.id} label={cat.label} />
          ))}
        </Tabs>
      </Box>

      {/* Plugin Grid */}
      <Grid container spacing={3}>
        {plugins.map(plugin => (
          <Grid item xs={12} sm={6} md={4} key={plugin.id}>
            <Card
              sx={{
                height: '100%',
                display: 'flex',
                flexDirection: 'column',
                cursor: 'pointer',
                '&:hover': { boxShadow: 4 }
              }}
              onClick={() => setSelectedPlugin(plugin)}
            >
              <CardContent sx={{ flexGrow: 1 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                  <Avatar src={plugin.icon} sx={{ mr: 2 }}>
                    {plugin.name.charAt(0)}
                  </Avatar>
                  <Box sx={{ flexGrow: 1 }}>
                    <Typography variant="h6">{plugin.name}</Typography>
                    <Typography variant="body2" color="text.secondary">
                      by {plugin.author}
                    </Typography>
                  </Box>
                  {plugin.signature_verified && (
                    <SecurityIcon color="success" titleAccess="Signature Verified" />
                  )}
                </Box>

                <Typography variant="body2" sx={{ mb: 2, height: 40, overflow: 'hidden' }}>
                  {plugin.description}
                </Typography>

                <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, mb: 2 }}>
                  {plugin.categories.map(category => (
                    <Chip key={category} label={category} size="small" variant="outlined" />
                  ))}
                  {plugin.hipaa_compliant && (
                    <Chip label="HIPAA" size="small" color="error" />
                  )}
                </Box>

                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <Box>
                    <Rating value={plugin.rating} readOnly size="small" />
                    <Typography variant="caption" sx={{ ml: 1 }}>
                      ({plugin.downloads} downloads)
                    </Typography>
                  </Box>
                  <Button
                    variant="contained"
                    size="small"
                    onClick={(e) => {
                      e.stopPropagation();
                      installPlugin(plugin.id);
                    }}
                    disabled={installing === plugin.id}
                    sx={{ borderRadius: 2 }}
                  >
                    {installing === plugin.id ? 'Installing...' : 'Install'}
                  </Button>
                </Box>
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>

      {/* Plugin Details Dialog */}
      <Dialog
        open={!!selectedPlugin}
        onClose={() => setSelectedPlugin(null)}
        maxWidth="md"
        fullWidth
      >
        {selectedPlugin && (
          <>
            <DialogTitle>
              <Box sx={{ display: 'flex', alignItems: 'center' }}>
                <Avatar src={selectedPlugin.icon} sx={{ mr: 2 }}>
                  {selectedPlugin.name.charAt(0)}
                </Avatar>
                <Box>
                  <Typography variant="h5">{selectedPlugin.name}</Typography>
                  <Typography variant="body2" color="text.secondary">
                    Version {selectedPlugin.version} by {selectedPlugin.author}
                  </Typography>
                </Box>
              </Box>
            </DialogTitle>
            <DialogContent>
              <Typography variant="body1" sx={{ mb: 2 }}>
                {selectedPlugin.description}
              </Typography>

              {selectedPlugin.hipaa_compliant && (
                <Alert severity="info" sx={{ mb: 2 }}>
                  This plugin is HIPAA-compliant and suitable for healthcare environments.
                </Alert>
              )}

              <Box sx={{ display: 'flex', gap: 1, mb: 2 }}>
                {selectedPlugin.categories.map(category => (
                  <Chip key={category} label={category} />
                ))}
              </Box>

              <Button
                variant="contained"
                fullWidth
                onClick={() => installPlugin(selectedPlugin.id)}
                disabled={installing === selectedPlugin.id}
                sx={{ mt: 2, borderRadius: 2 }}
              >
                {installing === selectedPlugin.id ? 'Installing...' : 'Install Plugin'}
              </Button>
            </DialogContent>
          </>
        )}
      </Dialog>
    </Box>
  );
}
```

## Week 3-4: Enterprise Identity Integration Extensions

### 4. LDAP/Active Directory Integration Plugin

**Create `api/app/plugins/identity/ldap/plugin.py`**:
```python
import ldap3
from typing import Dict, Any, List, Optional
from api.app.plugins.identity.base import IdentityProvider, User, Group, AuthResult
from datetime import datetime, timedelta

class LDAPIdentityProvider(IdentityProvider):
    """Enterprise LDAP/Active Directory integration"""

    def __init__(self, security_core, audit_logger, trait_engine, manifest):
        super().__init__(security_core, audit_logger, trait_engine, manifest)
        self.server = None
        self.connection = None
        self.config = {}

    async def initialize(self) -> bool:
        """Initialize LDAP connection"""
        try:
            self.config = await self.get_config()

            # Setup LDAP server connection
            server_uri = self.config['server_uri']  # ldaps://dc.company.com:636
            self.server = ldap3.Server(
                server_uri,
                use_ssl=server_uri.startswith('ldaps://'),
                get_info=ldap3.ALL
            )

            # Test service account connection
            service_dn = self.config['service_account_dn']
            service_password = self.config['service_account_password']

            conn = ldap3.Connection(
                self.server,
                user=service_dn,
                password=service_password,
                auto_bind=True,
                authentication=ldap3.NTLM if self.config.get('use_ntlm') else ldap3.SIMPLE
            )

            if conn.bind():
                self.connection = conn
                self.log_event('ldap_initialized', audit_level='STANDARD',
                              server=server_uri, service_account=service_dn)
                return True
            else:
                raise ConnectionError("Failed to bind to LDAP server")

        except Exception as e:
            self.log_event('ldap_initialization_failed', audit_level='CRITICAL', error=str(e))
            return False

    async def authenticate(self, credentials: Dict[str, Any]) -> AuthResult:
        """Authenticate user against LDAP/AD"""
        username = credentials.get('username')
        password = credentials.get('password')

        if not username or not password:
            return AuthResult(success=False, error="Username and password required")

        try:
            # Find user DN in directory
            user_dn = await self._find_user_dn(username)
            if not user_dn:
                self.log_event('ldap_user_not_found', audit_level='HIGH', username=username)
                return AuthResult(success=False, error="Invalid credentials")

            # Attempt bind with user credentials
            user_conn = ldap3.Connection(
                self.server,
                user=user_dn,
                password=password,
                authentication=ldap3.NTLM if self.config.get('use_ntlm') else ldap3.SIMPLE
            )

            if not user_conn.bind():
                self.log_event('ldap_auth_failed', audit_level='HIGH',
                              username=username, user_dn=user_dn)
                return AuthResult(success=False, error="Invalid credentials")

            # Get user attributes and groups
            user_attributes = await self._get_user_attributes(user_dn)
            user_groups = await self._get_user_groups(user_dn)

            # Map LDAP attributes to Faxbot user
            user = await self._map_ldap_user(user_attributes, user_groups)

            self.log_event('ldap_auth_success', audit_level='STANDARD',
                          username=username, user_id=user.id, groups=user.groups)

            return AuthResult(success=True, user=user)

        except Exception as e:
            self.log_event('ldap_auth_error', audit_level='HIGH',
                          username=username, error=str(e))
            return AuthResult(success=False, error="Authentication failed")

    async def _find_user_dn(self, username: str) -> Optional[str]:
        """Find user's distinguished name in LDAP directory"""
        base_dn = self.config['user_base_dn']  # e.g., "OU=Users,DC=company,DC=com"
        search_filter = self.config['user_search_filter'].format(username=username)
        # e.g., "(&(objectClass=person)(sAMAccountName={username}))"

        self.connection.search(
            search_base=base_dn,
            search_filter=search_filter,
            attributes=['distinguishedName']
        )

        if self.connection.entries:
            return str(self.connection.entries[0].distinguishedName)
        return None

    async def _get_user_attributes(self, user_dn: str) -> Dict[str, Any]:
        """Get user attributes from LDAP"""
        self.connection.search(
            search_base=user_dn,
            search_filter='(objectClass=*)',
            attributes=[
                'sAMAccountName', 'userPrincipalName', 'displayName',
                'mail', 'department', 'title', 'memberOf'
            ]
        )

        if self.connection.entries:
            entry = self.connection.entries[0]
            return {attr: entry[attr].value for attr in entry.entry_attributes}
        return {}

    async def _map_ldap_user(self, attributes: Dict, groups: List[str]) -> User:
        """Map LDAP attributes to Faxbot user object"""
        # Map LDAP groups to Faxbot traits
        traits = []

        # Department-based trait mapping
        dept = attributes.get('department', '').lower()
        if dept in ['medical', 'clinical', 'nursing']:
            traits.extend(['hipaa_compliant', 'phi_authorized', 'fax_capable'])
        elif dept in ['marketing', 'sales']:
            traits.extend(['non_hipaa', 'fax_capable', 'email_capable'])
        elif dept in ['it', 'admin']:
            traits.extend(['admin_capable', 'fax_capable', 'email_capable'])

        # Group-based trait mapping from config
        group_trait_map = self.config.get('group_trait_mapping', {})
        for group in groups:
            if group in group_trait_map:
                traits.extend(group_trait_map[group])

        # Ensure unique traits
        traits = list(set(traits))

        return User(
            id=f"ldap_{attributes.get('sAMAccountName')}",
            username=attributes.get('sAMAccountName', ''),
            email=attributes.get('mail', ''),
            full_name=attributes.get('displayName', ''),
            is_active=True,
            traits=traits,
            groups=groups,
            metadata={
                'department': attributes.get('department'),
                'title': attributes.get('title'),
                'ldap_dn': attributes.get('distinguishedName'),
                'source': 'ldap'
            },
            created_at=datetime.utcnow()  # LDAP sync time
        )
```

### 5. SAML 2.0 SSO Integration Plugin

**Create `api/app/plugins/identity/saml/plugin.py`**:
```python
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.settings import OneLogin_Saml2_Settings
from onelogin.saml2.utils import OneLogin_Saml2_Utils
from typing import Dict, Any, Optional
from api.app.plugins.identity.base import IdentityProvider, User, AuthResult

class SAMLIdentityProvider(IdentityProvider):
    """SAML 2.0 SSO integration for enterprise identity providers"""

    def __init__(self, security_core, audit_logger, trait_engine, manifest):
        super().__init__(security_core, audit_logger, trait_engine, manifest)
        self.saml_settings = None

    async def initialize(self) -> bool:
        """Initialize SAML configuration"""
        try:
            config = await self.get_config()

            # SAML settings configuration
            self.saml_settings = {
                "sp": {
                    "entityId": config['sp_entity_id'],  # https://faxbot.company.com/saml/metadata
                    "assertionConsumerService": {
                        "url": config['sp_acs_url'],  # https://faxbot.company.com/auth/saml/acs
                        "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
                    },
                    "singleLogoutService": {
                        "url": config['sp_sls_url'],  # https://faxbot.company.com/auth/saml/logout
                        "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
                    },
                    "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
                    "x509cert": config.get('sp_x509cert', ''),
                    "privateKey": config.get('sp_private_key', '')
                },
                "idp": {
                    "entityId": config['idp_entity_id'],  # https://company.okta.com/saml2/service-provider
                    "singleSignOnService": {
                        "url": config['idp_sso_url'],
                        "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
                    },
                    "singleLogoutService": {
                        "url": config['idp_sls_url'],
                        "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
                    },
                    "x509cert": config['idp_x509cert']
                }
            }

            # Validate SAML settings
            settings_obj = OneLogin_Saml2_Settings(self.saml_settings)
            if not settings_obj.check_settings():
                raise ValueError("Invalid SAML settings configuration")

            self.log_event('saml_initialized', audit_level='STANDARD',
                          sp_entity_id=config['sp_entity_id'])
            return True

        except Exception as e:
            self.log_event('saml_initialization_failed', audit_level='CRITICAL', error=str(e))
            return False

    async def initiate_sso(self, request_data: Dict[str, Any]) -> str:
        """Initiate SAML SSO login"""
        auth = OneLogin_Saml2_Auth(request_data, self.saml_settings)
        sso_url = auth.login(return_to=request_data.get('return_to'))

        self.log_event('saml_sso_initiated', audit_level='STANDARD',
                      return_to=request_data.get('return_to'))
        return sso_url

    async def process_saml_response(self, request_data: Dict[str, Any]) -> AuthResult:
        """Process SAML assertion response"""
        try:
            auth = OneLogin_Saml2_Auth(request_data, self.saml_settings)
            auth.process_response()

            errors = auth.get_errors()
            if errors:
                error_msg = f"SAML errors: {', '.join(errors)}"
                self.log_event('saml_response_error', audit_level='HIGH', errors=errors)
                return AuthResult(success=False, error=error_msg)

            if not auth.is_authenticated():
                self.log_event('saml_auth_failed', audit_level='HIGH',
                              reason='not_authenticated')
                return AuthResult(success=False, error="Authentication failed")

            # Extract user attributes from SAML assertion
            attributes = auth.get_attributes()
            nameid = auth.get_nameid()
            session_index = auth.get_session_index()

            # Map SAML attributes to Faxbot user
            user = await self._map_saml_user(nameid, attributes)

            self.log_event('saml_auth_success', audit_level='STANDARD',
                          nameid=nameid, user_id=user.id)

            return AuthResult(
                success=True,
                user=user,
                metadata={
                    'saml_session_index': session_index,
                    'saml_nameid': nameid
                }
            )

        except Exception as e:
            self.log_event('saml_processing_error', audit_level='HIGH', error=str(e))
            return AuthResult(success=False, error="SAML processing failed")

    async def _map_saml_user(self, nameid: str, attributes: Dict[str, List[str]]) -> User:
        """Map SAML attributes to Faxbot user"""
        config = await self.get_config()
        attr_map = config.get('attribute_mapping', {})

        # Extract mapped attributes
        email = self._get_saml_attr(attributes, attr_map.get('email', 'email'), nameid)
        full_name = self._get_saml_attr(attributes, attr_map.get('full_name', 'displayName'), '')
        department = self._get_saml_attr(attributes, attr_map.get('department', 'department'), '')
        groups = attributes.get(attr_map.get('groups', 'groups'), [])

        # Department and group-based trait mapping
        traits = []
        if department.lower() in ['medical', 'clinical']:
            traits.extend(['hipaa_compliant', 'phi_authorized', 'fax_capable'])
        elif department.lower() in ['marketing', 'sales']:
            traits.extend(['non_hipaa', 'fax_capable'])
        elif 'admin' in [g.lower() for g in groups]:
            traits.extend(['admin_capable', 'fax_capable'])

        return User(
            id=f"saml_{nameid}",
            username=email.split('@')[0] if '@' in email else nameid,
            email=email,
            full_name=full_name,
            is_active=True,
            traits=list(set(traits)),
            groups=groups,
            metadata={
                'department': department,
                'saml_nameid': nameid,
                'source': 'saml'
            },
            created_at=datetime.utcnow()
        )

    def _get_saml_attr(self, attributes: Dict, attr_name: str, default: str = '') -> str:
        """Get SAML attribute value with fallback"""
        attr_values = attributes.get(attr_name, [])
        return attr_values[0] if attr_values else default
```

## Week 5-6: Advanced Event System & Webhooks

### 6. Enterprise Event Bus Extension

**Extend `api/app/events/bus.py`**:
```python
from typing import Dict, Any, List, Optional, Callable, AsyncGenerator
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import asyncio
import json
import redis.asyncio as aioredis
from api.app.core.audit import audit_event

@dataclass
class EnterpriseEvent:
    """Enhanced event with tenant isolation and compliance"""
    id: str
    type: str
    source: str
    tenant_id: str
    user_id: Optional[str]
    timestamp: datetime
    data: Dict[str, Any]
    correlation_id: Optional[str] = None
    compliance_level: str = 'standard'  # 'standard', 'hipaa', 'pci'
    retry_count: int = 0
    metadata: Dict[str, Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'EnterpriseEvent':
        # Convert timestamp string back to datetime
        if isinstance(data.get('timestamp'), str):
            data['timestamp'] = datetime.fromisoformat(data['timestamp'])
        return cls(**data)

class EnterpriseEventBus:
    """Advanced event bus with tenant isolation and compliance filtering"""

    def __init__(self, redis_client, security_core, trait_engine):
        self.redis = redis_client
        self.security_core = security_core
        self.trait_engine = trait_engine
        self.subscribers = {}  # pattern -> handler mapping
        self.dead_letter_queue = "events:dlq"

    async def publish(self, event: EnterpriseEvent, tenant_context: Dict[str, Any]) -> bool:
        """Publish event with tenant isolation and compliance filtering"""
        try:
            # Validate tenant permissions
            if not await self._validate_publish_permissions(event, tenant_context):
                audit_event('event_publish_denied', tenant_id=event.tenant_id,
                          event_type=event.type, reason='insufficient_permissions')
                return False

            # Apply compliance filtering
            filtered_event = await self._apply_compliance_filter(event, tenant_context)

            # Publish to tenant-specific channels
            tenant_channel = f"events:{event.tenant_id}:{event.type}"
            global_channel = f"events:global:{event.type}"

            event_json = json.dumps(filtered_event.to_dict(), default=str)

            # Publish to both channels
            await self.redis.publish(tenant_channel, event_json)

            # Only publish to global if not PHI/sensitive
            if filtered_event.compliance_level == 'standard':
                await self.redis.publish(global_channel, event_json)

            # Store in event log for replay
            await self._store_event_log(filtered_event)

            audit_event('event_published', tenant_id=event.tenant_id,
                       event_type=event.type, event_id=event.id)
            return True

        except Exception as e:
            audit_event('event_publish_failed', tenant_id=event.tenant_id,
                       event_type=event.type, error=str(e))
            return False

    async def subscribe(self, pattern: str, handler: Callable, tenant_id: str,
                       compliance_filter: Optional[str] = None) -> str:
        """Subscribe to events with tenant and compliance filtering"""
        subscription_id = f"sub_{tenant_id}_{len(self.subscribers)}"

        self.subscribers[subscription_id] = {
            'pattern': pattern,
            'handler': handler,
            'tenant_id': tenant_id,
            'compliance_filter': compliance_filter,
            'created_at': datetime.utcnow()
        }

        # Start listening to tenant-specific channel
        tenant_channel = f"events:{tenant_id}:{pattern}"
        asyncio.create_task(self._handle_subscription(subscription_id, tenant_channel))

        return subscription_id

    async def replay_events(self, from_timestamp: datetime, to_timestamp: datetime,
                          tenant_id: str, event_filter: Optional[str] = None) -> AsyncGenerator[EnterpriseEvent, None]:
        """Replay events from event log with filtering"""
        # Query event log from Redis Streams or TimeSeries
        stream_key = f"eventlog:{tenant_id}"

        # Convert timestamps to Redis stream IDs
        from_id = f"{int(from_timestamp.timestamp() * 1000)}-0"
        to_id = f"{int(to_timestamp.timestamp() * 1000)}-0"

        try:
            events = await self.redis.xrange(stream_key, min=from_id, max=to_id)

            for event_id, fields in events:
                try:
                    event_data = json.loads(fields[b'data'].decode())
                    event = EnterpriseEvent.from_dict(event_data)

                    # Apply event filter if specified
                    if not event_filter or event.type.match(event_filter):
                        yield event

                except Exception as e:
                    # Skip malformed events but log the issue
                    audit_event('event_replay_skip', tenant_id=tenant_id,
                               event_id=event_id, error=str(e))
                    continue

        except Exception as e:
            audit_event('event_replay_failed', tenant_id=tenant_id,
                       from_timestamp=from_timestamp.isoformat(),
                       to_timestamp=to_timestamp.isoformat(), error=str(e))

    async def _apply_compliance_filter(self, event: EnterpriseEvent,
                                     tenant_context: Dict[str, Any]) -> EnterpriseEvent:
        """Apply compliance-specific filtering to event data"""
        if event.compliance_level == 'hipaa':
            # Remove PHI from event data
            filtered_data = self._redact_phi(event.data)
            event.data = filtered_data

        elif event.compliance_level == 'pci':
            # Remove payment card data
            filtered_data = self._redact_pci_data(event.data)
            event.data = filtered_data

        return event

    def _redact_phi(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Redact PHI from event data"""
        # Implementation would use privacy linter patterns
        from api.app.core.privacy_linter import PrivacyLinter
        linter = PrivacyLinter()
        return linter.redact_sensitive_data(data)
```

### 7. Enhanced Webhook Delivery System

**Extend `api/app/events/webhooks.py`**:
```python
import asyncio
import aiohttp
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass
import hmac
import hashlib
import json

@dataclass
class WebhookEndpoint:
    """Webhook endpoint configuration"""
    id: str
    tenant_id: str
    url: str
    secret: str
    event_types: List[str]
    enabled: bool = True
    max_retries: int = 3
    timeout_seconds: int = 30
    headers: Dict[str, str] = None
    compliance_level: str = 'standard'

class EnterpriseWebhookDelivery:
    """Enhanced webhook delivery with circuit breakers and compliance"""

    def __init__(self, event_bus, redis_client, security_core):
        self.event_bus = event_bus
        self.redis = redis_client
        self.security_core = security_core
        self.circuit_breakers = {}  # endpoint_id -> circuit breaker state
        self.delivery_queue = "webhooks:delivery"
        self.dlq = "webhooks:dlq"

    async def register_webhook(self, endpoint: WebhookEndpoint,
                             tenant_context: Dict[str, Any]) -> bool:
        """Register webhook endpoint with validation"""
        try:
            # Validate URL and accessibility
            if not await self._validate_webhook_url(endpoint.url):
                return False

            # Store endpoint configuration
            endpoint_key = f"webhook_endpoints:{endpoint.tenant_id}:{endpoint.id}"
            await self.redis.hset(endpoint_key, mapping={
                'config': json.dumps(endpoint.__dict__, default=str),
                'created_at': datetime.utcnow().isoformat(),
                'status': 'active'
            })

            # Initialize circuit breaker
            self.circuit_breakers[endpoint.id] = {
                'state': 'closed',  # closed, open, half-open
                'failure_count': 0,
                'last_failure': None,
                'next_attempt': None
            }

            audit_event('webhook_registered', tenant_id=endpoint.tenant_id,
                       endpoint_id=endpoint.id, url=endpoint.url)
            return True

        except Exception as e:
            audit_event('webhook_registration_failed', tenant_id=endpoint.tenant_id,
                       endpoint_id=endpoint.id, error=str(e))
            return False

    async def deliver_webhook(self, event: EnterpriseEvent,
                            endpoint: WebhookEndpoint) -> bool:
        """Deliver webhook with circuit breaker and retries"""

        # Check circuit breaker state
        if not await self._check_circuit_breaker(endpoint.id):
            audit_event('webhook_circuit_breaker_open',
                       endpoint_id=endpoint.id, tenant_id=event.tenant_id)
            return False

        try:
            # Generate webhook signature
            payload = json.dumps(event.to_dict(), default=str)
            signature = self._generate_signature(payload, endpoint.secret)

            # Prepare headers
            headers = {
                'Content-Type': 'application/json',
                'X-Faxbot-Signature': signature,
                'X-Faxbot-Event-Type': event.type,
                'X-Faxbot-Event-Id': event.id,
                'X-Faxbot-Timestamp': event.timestamp.isoformat(),
                'User-Agent': 'Faxbot-Webhook/1.0'
            }

            if endpoint.headers:
                headers.update(endpoint.headers)

            # Apply compliance filtering to payload
            if endpoint.compliance_level == 'hipaa' and event.compliance_level == 'hipaa':
                # Use filtered/redacted event data
                filtered_event = await self.event_bus._apply_compliance_filter(event, {})
                payload = json.dumps(filtered_event.to_dict(), default=str)

            # Deliver webhook
            timeout = aiohttp.ClientTimeout(total=endpoint.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    endpoint.url,
                    data=payload,
                    headers=headers,
                    ssl=True  # Always verify SSL
                ) as response:
                    if response.status >= 200 and response.status < 300:
                        # Success - reset circuit breaker
                        await self._reset_circuit_breaker(endpoint.id)

                        audit_event('webhook_delivered', endpoint_id=endpoint.id,
                                   tenant_id=event.tenant_id, event_id=event.id,
                                   status_code=response.status)
                        return True
                    else:
                        # HTTP error - record failure
                        await self._record_failure(endpoint.id, f"HTTP {response.status}")
                        return False

        except asyncio.TimeoutError:
            await self._record_failure(endpoint.id, "timeout")
            return False
        except Exception as e:
            await self._record_failure(endpoint.id, str(e))
            return False

    async def _check_circuit_breaker(self, endpoint_id: str) -> bool:
        """Check if circuit breaker allows requests"""
        breaker = self.circuit_breakers.get(endpoint_id)
        if not breaker:
            return True

        if breaker['state'] == 'closed':
            return True
        elif breaker['state'] == 'open':
            # Check if enough time has passed to try again
            if breaker['next_attempt'] and datetime.utcnow() >= breaker['next_attempt']:
                breaker['state'] = 'half-open'
                return True
            return False
        elif breaker['state'] == 'half-open':
            # Allow one request to test if service is back
            return True

        return False

    async def _record_failure(self, endpoint_id: str, error: str):
        """Record webhook failure and update circuit breaker"""
        breaker = self.circuit_breakers.get(endpoint_id, {
            'state': 'closed', 'failure_count': 0, 'last_failure': None, 'next_attempt': None
        })

        breaker['failure_count'] += 1
        breaker['last_failure'] = datetime.utcnow()

        # Open circuit breaker after 5 consecutive failures
        if breaker['failure_count'] >= 5:
            breaker['state'] = 'open'
            # Wait 5 minutes before trying again
            breaker['next_attempt'] = datetime.utcnow() + timedelta(minutes=5)

        self.circuit_breakers[endpoint_id] = breaker

        audit_event('webhook_failure', endpoint_id=endpoint_id,
                   error=error, failure_count=breaker['failure_count'])

    def _generate_signature(self, payload: str, secret: str) -> str:
        """Generate HMAC signature for webhook verification"""
        signature = hmac.new(
            secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()
        return f"sha256={signature}"
```

## Week 7: Plugin Development SDK & API Gateway

### 8. Complete Plugin Development SDK

**Enhance `plugin-dev-kit/python/faxbot_plugin_dev/cli.py`**:
```python
import click
import os
import json
import shutil
from pathlib import Path
import subprocess
from typing import Dict, Any

@click.group()
@click.version_option(version='1.0.0')
def cli():
    """Faxbot Plugin Development Kit"""
    pass

@cli.command()
@click.argument('plugin_name')
@click.option('--type', 'plugin_type',
              type=click.Choice(['transport', 'storage', 'identity', 'messaging', 'integration']),
              required=True, help='Type of plugin to create')
@click.option('--author', default='', help='Plugin author name')
@click.option('--hipaa-compliant', is_flag=True, help='Mark plugin as HIPAA compliant')
def init(plugin_name: str, plugin_type: str, author: str, hipaa_compliant: bool):
    """Initialize a new plugin project"""
    plugin_dir = Path(plugin_name)

    if plugin_dir.exists():
        click.echo(f"Error: Directory {plugin_name} already exists", err=True)
        return

    # Create plugin directory structure
    plugin_dir.mkdir()
    (plugin_dir / 'src').mkdir()
    (plugin_dir / 'tests').mkdir()
    (plugin_dir / 'docs').mkdir()

    # Generate manifest.json
    manifest = {
        "id": plugin_name.lower().replace(' ', '_'),
        "name": plugin_name,
        "version": "1.0.0",
        "description": f"A {plugin_type} plugin for Faxbot",
        "author": author or "Plugin Developer",
        "categories": [plugin_type],
        "capabilities": _get_default_capabilities(plugin_type),
        "homepage": "",
        "license": "MIT",
        "hipaa_compliant": hipaa_compliant,
        "requires_baa": hipaa_compliant,
        "config_schema": _get_default_config_schema(plugin_type)
    }

    with open(plugin_dir / 'manifest.json', 'w') as f:
        json.dump(manifest, f, indent=2)

    # Generate plugin template
    template_content = _generate_plugin_template(plugin_name, plugin_type, hipaa_compliant)
    with open(plugin_dir / 'src' / 'plugin.py', 'w') as f:
        f.write(template_content)

    # Generate test template
    test_content = _generate_test_template(plugin_name, plugin_type)
    with open(plugin_dir / 'tests' / 'test_plugin.py', 'w') as f:
        f.write(test_content)

    # Generate setup.py
    setup_content = _generate_setup_py(plugin_name, manifest)
    with open(plugin_dir / 'setup.py', 'w') as f:
        f.write(setup_content)

    # Generate README
    readme_content = _generate_readme(plugin_name, plugin_type, manifest)
    with open(plugin_dir / 'README.md', 'w') as f:
        f.write(readme_content)

    click.echo(f"✅ Plugin '{plugin_name}' created successfully!")
    click.echo(f"   Directory: {plugin_dir.absolute()}")
    click.echo("   Next steps:")
    click.echo("   1. cd " + plugin_name)
    click.echo("   2. faxbot-sdk validate")
    click.echo("   3. faxbot-sdk test")

@cli.command()
@click.option('--manifest', default='manifest.json', help='Path to manifest file')
def validate(manifest: str):
    """Validate plugin manifest and structure"""
    if not os.path.exists(manifest):
        click.echo(f"Error: Manifest file {manifest} not found", err=True)
        return

    try:
        with open(manifest) as f:
            manifest_data = json.load(f)

        # Validate required fields
        required_fields = ['id', 'name', 'version', 'description', 'author', 'categories', 'capabilities']
        missing_fields = [field for field in required_fields if field not in manifest_data]

        if missing_fields:
            click.echo(f"❌ Missing required fields: {', '.join(missing_fields)}", err=True)
            return

        # Validate plugin structure
        src_dir = Path('src')
        if not src_dir.exists():
            click.echo("❌ Missing src/ directory", err=True)
            return

        plugin_file = src_dir / 'plugin.py'
        if not plugin_file.exists():
            click.echo("❌ Missing src/plugin.py file", err=True)
            return

        # Validate Python syntax
        try:
            subprocess.run(['python', '-m', 'py_compile', str(plugin_file)],
                          check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            click.echo(f"❌ Python syntax error in plugin.py: {e.stderr.decode()}", err=True)
            return

        click.echo("✅ Plugin validation passed!")

    except json.JSONDecodeError as e:
        click.echo(f"❌ Invalid JSON in manifest: {e}", err=True)
    except Exception as e:
        click.echo(f"❌ Validation error: {e}", err=True)

@cli.command()
@click.option('--coverage', is_flag=True, help='Run with coverage report')
def test(coverage: bool):
    """Run plugin tests"""
    if not os.path.exists('tests/'):
        click.echo("❌ No tests/ directory found", err=True)
        return

    try:
        cmd = ['python', '-m', 'pytest', 'tests/']
        if coverage:
            cmd.extend(['--cov=src', '--cov-report=term-missing'])

        result = subprocess.run(cmd, capture_output=True, text=True)

        click.echo(result.stdout)
        if result.stderr:
            click.echo(result.stderr, err=True)

        if result.returncode == 0:
            click.echo("✅ All tests passed!")
        else:
            click.echo("❌ Some tests failed", err=True)

    except Exception as e:
        click.echo(f"❌ Test execution error: {e}", err=True)

@cli.command()
@click.option('--host', default='localhost', help='Development server host')
@click.option('--port', default=8081, help='Development server port')
def serve(host: str, port: int):
    """Start local development server"""
    click.echo(f"🚀 Starting development server at http://{host}:{port}")
    click.echo("   This will start a local Faxbot instance for testing your plugin")

    # Start development server with hot reload
    try:
        subprocess.run([
            'python', '-m', 'uvicorn',
            'faxbot_plugin_dev.dev_server:app',
            '--host', host,
            '--port', str(port),
            '--reload'
        ])
    except KeyboardInterrupt:
        click.echo("\n👋 Development server stopped")

def _generate_plugin_template(plugin_name: str, plugin_type: str, hipaa_compliant: bool) -> str:
    """Generate plugin template based on type"""

    base_imports = [
        "from typing import Dict, Any, Optional, List",
        "from datetime import datetime",
        "import logging"
    ]

    if plugin_type == 'transport':
        base_imports.append("from faxbot_plugin_dev.base import FaxPlugin, SendResult, StatusResult")
        base_class = "FaxPlugin"
    elif plugin_type == 'storage':
        base_imports.append("from faxbot_plugin_dev.base import StoragePlugin")
        base_class = "StoragePlugin"
    elif plugin_type == 'identity':
        base_imports.append("from faxbot_plugin_dev.base import AuthPlugin")
        base_class = "AuthPlugin"
    else:
        base_imports.append("from faxbot_plugin_dev.base import PluginBase")
        base_class = "PluginBase"

    template = f'''"""
{plugin_name} - A {plugin_type} plugin for Faxbot
{"HIPAA Compliant Implementation" if hipaa_compliant else ""}
"""

{chr(10).join(base_imports)}

logger = logging.getLogger(__name__)

class {plugin_name.replace(" ", "")}Plugin({base_class}):
    """
    {plugin_name} plugin implementation
    {"This plugin is designed for HIPAA-compliant environments" if hipaa_compliant else ""}
    """

    def __init__(self, deps=None):
        super().__init__(deps)
        self.logger = logger

    def manifest(self):
        """Return plugin manifest"""
        from faxbot_plugin_dev.base import PluginManifest
        return PluginManifest(
            id="{plugin_name.lower().replace(' ', '_')}",
            name="{plugin_name}",
            version="1.0.0",
            description="A {plugin_type} plugin for Faxbot",
            author="Plugin Developer",
            categories=["{plugin_type}"],
            capabilities={_get_default_capabilities(plugin_type)},
            hipaa_compliant={str(hipaa_compliant).lower()},
            requires_baa={str(hipaa_compliant).lower()}
        )

    def validate_config(self, config: Dict[str, Any]) -> None:
        """Validate plugin configuration"""
        # Add your configuration validation here
        required_keys = []  # Add required config keys

        for key in required_keys:
            if key not in config:
                raise ValueError(f"Missing required configuration key: {{key}}")

    async def initialize(self, config: Dict[str, Any]) -> None:
        """Initialize the plugin"""
        self.validate_config(config)
        self.config = config

        # Add your initialization logic here
        self.logger.info(f"{plugin_name} plugin initialized")
        self._initialized = True

    async def shutdown(self) -> None:
        """Shutdown the plugin"""
        # Add cleanup logic here
        self.logger.info(f"{plugin_name} plugin shutting down")
        self._initialized = False

{_get_plugin_methods(plugin_type, hipaa_compliant)}
'''

    return template

def _get_plugin_methods(plugin_type: str, hipaa_compliant: bool) -> str:
    """Get plugin-specific method implementations"""

    if plugin_type == 'transport':
        return '''
    async def send_fax(self, to_number: str, file_path: str, options: Optional[Dict[str, Any]] = None) -> SendResult:
        """Send fax implementation"""
        # TODO: Implement fax sending logic

        job_id = f"job_{datetime.now().timestamp()}"

        # Add your fax sending implementation here

        return SendResult(
            job_id=job_id,
            backend=self.manifest().id,
            provider_sid="provider_job_id",
            estimated_cost=0.10,
            estimated_duration=60
        )

    async def get_status(self, job_id: str) -> StatusResult:
        """Get fax status implementation"""
        # TODO: Implement status checking logic

        return StatusResult(
            job_id=job_id,
            status="SUCCESS",
            pages=1,
            duration=45,
            cost=0.10
        )
        '''

    elif plugin_type == 'storage':
        return '''
    async def put(self, path: str, data: bytes, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Store data implementation"""
        # TODO: Implement data storage logic
        return f"stored://{path}"

    async def get(self, path: str) -> bytes:
        """Retrieve data implementation"""
        # TODO: Implement data retrieval logic
        return b"file content"

    async def delete(self, path: str) -> bool:
        """Delete data implementation"""
        # TODO: Implement data deletion logic
        return True

    async def exists(self, path: str) -> bool:
        """Check if data exists implementation"""
        # TODO: Implement existence check logic
        return True

    async def list(self, prefix: str = "") -> List[str]:
        """List stored data implementation"""
        # TODO: Implement data listing logic
        return []
        '''

    elif plugin_type == 'identity':
        return '''
    async def authenticate(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        """Authenticate user implementation"""
        # TODO: Implement authentication logic
        return {
            "success": True,
            "user_id": "user123",
            "metadata": {}
        }

    async def validate_token(self, token: str) -> bool:
        """Validate token implementation"""
        # TODO: Implement token validation logic
        return True

    async def refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        """Refresh token implementation"""
        # TODO: Implement token refresh logic
        return {
            "access_token": "new_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600
        }
        '''

    return ""

def _get_default_capabilities(plugin_type: str) -> List[str]:
    """Get default capabilities for plugin type"""
    capabilities_map = {
        'transport': ['send', 'get_status'],
        'storage': ['store', 'retrieve', 'delete'],
        'identity': ['authenticate', 'validate'],
        'messaging': ['send_message', 'get_status'],
        'integration': ['sync', 'webhook']
    }
    return capabilities_map.get(plugin_type, ['basic'])

def _get_default_config_schema(plugin_type: str) -> Dict[str, Any]:
    """Get default configuration schema for plugin type"""
    schemas = {
        'transport': {
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {"type": "string", "title": "API Key", "secret": True},
                "endpoint": {"type": "string", "title": "API Endpoint", "default": ""}
            }
        },
        'storage': {
            "type": "object",
            "required": ["connection_string"],
            "properties": {
                "connection_string": {"type": "string", "title": "Connection String", "secret": True}
            }
        }
    }
    return schemas.get(plugin_type, {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean", "default": True}
        }
    })

if __name__ == '__main__':
    cli()
```

### 9. Enterprise API Gateway

**Create `api/app/gateway/enterprise.py`**:
```python
from fastapi import FastAPI, Request, HTTPException, Depends
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Dict, Any, Optional
import time
import asyncio
import redis.asyncio as aioredis
from datetime import datetime, timedelta
import json

class EnterpriseAPIGateway:
    """Enterprise API gateway with per-tenant rate limiting and analytics"""

    def __init__(self, redis_client, hierarchical_config, security_core):
        self.redis = redis_client
        self.config = hierarchical_config
        self.security = security_core
        self.rate_limits = {}  # tenant_id -> rate limit config
        self.analytics = {}    # tenant_id -> usage analytics

    async def check_rate_limit(self, tenant_id: str, endpoint: str,
                              user_context: Dict[str, Any]) -> bool:
        """Check per-tenant rate limiting"""

        # Get tenant-specific rate limit configuration
        rate_config = await self.config.get_effective('api.rate_limits', {
            'tenant_id': tenant_id,
            'endpoint': endpoint
        })

        if not rate_config:
            # Default rate limits
            rate_config = {
                'requests_per_minute': 100,
                'burst_limit': 200,
                'per_user_limit': 20
            }

        # Check tenant-level rate limit
        tenant_key = f"rate_limit:tenant:{tenant_id}"
        current_minute = int(time.time() // 60)

        tenant_count = await self.redis.get(f"{tenant_key}:{current_minute}")
        tenant_count = int(tenant_count) if tenant_count else 0

        if tenant_count >= rate_config['requests_per_minute']:
            await self._record_rate_limit_violation(tenant_id, 'tenant_limit', endpoint)
            return False

        # Check user-level rate limit
        user_id = user_context.get('user_id')
        if user_id:
            user_key = f"rate_limit:user:{tenant_id}:{user_id}"
            user_count = await self.redis.get(f"{user_key}:{current_minute}")
            user_count = int(user_count) if user_count else 0

            if user_count >= rate_config['per_user_limit']:
                await self._record_rate_limit_violation(tenant_id, 'user_limit', endpoint, user_id)
                return False

        # Increment counters
        async with self.redis.pipeline() as pipe:
            pipe.incr(f"{tenant_key}:{current_minute}")
            pipe.expire(f"{tenant_key}:{current_minute}", 120)  # 2 minutes TTL

            if user_id:
                pipe.incr(f"{user_key}:{current_minute}")
                pipe.expire(f"{user_key}:{current_minute}", 120)

            await pipe.execute()

        return True

    async def record_api_usage(self, tenant_id: str, endpoint: str,
                              method: str, status_code: int,
                              response_time: float, user_context: Dict[str, Any]):
        """Record API usage analytics"""

        usage_data = {
            'timestamp': datetime.utcnow().isoformat(),
            'tenant_id': tenant_id,
            'endpoint': endpoint,
            'method': method,
            'status_code': status_code,
            'response_time': response_time,
            'user_id': user_context.get('user_id'),
            'user_agent': user_context.get('user_agent', ''),
            'ip_address': user_context.get('ip_address', ''),
        }

        # Store in time-series for analytics
        await self.redis.xadd(
            f"api_analytics:{tenant_id}",
            usage_data,
            maxlen=10000  # Keep last 10k entries per tenant
        )

        # Update usage counters
        date_key = datetime.utcnow().strftime('%Y-%m-%d')
        hour_key = datetime.utcnow().strftime('%Y-%m-%d:%H')

        async with self.redis.pipeline() as pipe:
            # Daily counters
            pipe.hincrby(f"usage_daily:{tenant_id}:{date_key}", endpoint, 1)
            pipe.hincrby(f"usage_daily:{tenant_id}:{date_key}", 'total_requests', 1)
            pipe.expire(f"usage_daily:{tenant_id}:{date_key}", 86400 * 31)  # 31 days

            # Hourly counters
            pipe.hincrby(f"usage_hourly:{tenant_id}:{hour_key}", endpoint, 1)
            pipe.expire(f"usage_hourly:{tenant_id}:{hour_key}", 86400 * 7)  # 7 days

            # Response time tracking
            if response_time > 0:
                pipe.lpush(f"response_times:{tenant_id}:{endpoint}", response_time)
                pipe.ltrim(f"response_times:{tenant_id}:{endpoint}", 0, 999)  # Keep last 1000

            await pipe.execute()

    async def get_usage_analytics(self, tenant_id: str,
                                 from_date: datetime, to_date: datetime) -> Dict[str, Any]:
        """Get usage analytics for tenant"""

        analytics = {
            'tenant_id': tenant_id,
            'period': {
                'from': from_date.isoformat(),
                'to': to_date.isoformat()
            },
            'total_requests': 0,
            'endpoints': {},
            'daily_breakdown': {},
            'avg_response_times': {},
            'status_codes': {},
            'top_users': {}
        }

        # Query daily usage data
        current_date = from_date.date()
        while current_date <= to_date.date():
            date_key = current_date.strftime('%Y-%m-%d')
            daily_data = await self.redis.hgetall(f"usage_daily:{tenant_id}:{date_key}")

            if daily_data:
                daily_usage = {k.decode(): int(v.decode()) for k, v in daily_data.items()}
                analytics['daily_breakdown'][date_key] = daily_usage
                analytics['total_requests'] += daily_usage.get('total_requests', 0)

                # Aggregate endpoint usage
                for endpoint, count in daily_usage.items():
                    if endpoint != 'total_requests':
                        analytics['endpoints'][endpoint] = analytics['endpoints'].get(endpoint, 0) + count

            current_date += timedelta(days=1)

        # Get response time analytics
        for endpoint in analytics['endpoints'].keys():
            response_times = await self.redis.lrange(f"response_times:{tenant_id}:{endpoint}", 0, -1)
            if response_times:
                times = [float(t.decode()) for t in response_times]
                analytics['avg_response_times'][endpoint] = sum(times) / len(times)

        return analytics

    async def _record_rate_limit_violation(self, tenant_id: str, violation_type: str,
                                         endpoint: str, user_id: Optional[str] = None):
        """Record rate limit violation for monitoring"""
        violation_data = {
            'timestamp': datetime.utcnow().isoformat(),
            'tenant_id': tenant_id,
            'violation_type': violation_type,
            'endpoint': endpoint,
            'user_id': user_id
        }

        await self.redis.xadd('rate_limit_violations', violation_data)

        # Could trigger alerts/notifications here
        from api.app.core.audit import audit_event
        audit_event('rate_limit_violation',
                   tenant_id=tenant_id,
                   violation_type=violation_type,
                   endpoint=endpoint,
                   user_id=user_id)

class APIGatewayMiddleware(BaseHTTPMiddleware):
    """Middleware for enterprise API gateway features"""

    def __init__(self, app, gateway: EnterpriseAPIGateway):
        super().__init__(app)
        self.gateway = gateway

    async def dispatch(self, request: Request, call_next):
        start_time = time.time()

        # Extract tenant context
        auth_context = getattr(request.state, 'auth_context', None)
        if not auth_context:
            # Skip gateway features for unauthenticated requests
            return await call_next(request)

        tenant_id = self._extract_tenant_id(auth_context)
        endpoint = f"{request.method} {request.url.path}"

        # Check rate limits
        user_context = {
            'user_id': auth_context.user_id,
            'user_agent': request.headers.get('user-agent', ''),
            'ip_address': request.client.host if request.client else ''
        }

        if not await self.gateway.check_rate_limit(tenant_id, endpoint, user_context):
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "Rate limit exceeded",
                    "retry_after": 60,
                    "tenant_id": tenant_id
                }
            )

        # Process request
        response = await call_next(request)

        # Record analytics
        response_time = (time.time() - start_time) * 1000  # Convert to milliseconds

        asyncio.create_task(
            self.gateway.record_api_usage(
                tenant_id, endpoint, request.method,
                response.status_code, response_time, user_context
            )
        )

        # Add response headers
        response.headers['X-RateLimit-Tenant'] = tenant_id
        response.headers['X-Response-Time'] = f"{response_time:.2f}ms"

        return response

    def _extract_tenant_id(self, auth_context) -> str:
        """Extract tenant ID from auth context"""
        # For Phase 4, tenant might come from user metadata or be derived
        tenant_id = auth_context.metadata.get('tenant_id')
        if not tenant_id:
            # Fallback to user-based tenant (one tenant per user for now)
            tenant_id = f"tenant_{auth_context.user_id}"
        return tenant_id
```

## Integration with All Existing Infrastructure

### 10. Comprehensive Integration Map

**All Existing Services Enhanced in Phase 4:**

#### Transport Providers (Enhanced, not Replaced):
- **Phaxio** (`api/app/phaxio_service.py`) → Marketplace integration + circuit breakers
- **Sinch** (`api/app/sinch_service.py`) → Enhanced with usage analytics
- **SignalWire** (`api/app/signalwire_service.py`) → Webhook hardening
- **SIP/Asterisk** (`api/app/ami.py`) → Enterprise monitoring integration
- **FreeSWITCH** (`api/app/freeswitch_service.py`) → Event bus integration

#### Existing Storage (Extended):
- **Local Storage** → Remains for development
- **S3 Storage** → Enhanced with multi-cloud replication

#### MCP Servers (Enhanced):
- **Node MCP** (`node_mcp/`) → Plugin-aware tools, enterprise integrations
- **Python MCP** (`python_mcp/`) → Advanced event streaming

#### Plugin Infrastructure (Preserved & Extended):
- **Plugin Manager** (`api/app/plugins/manager.py`) → Marketplace integration
- **Plugin Registry** (`config/plugin_registry.json`) → Signature verification
- **Provider Traits** (`config/provider_traits.json`) → Enterprise compliance traits

#### Configuration System (Enhanced):
- **HybridConfigProvider** (Phase 1) → Enterprise multi-tenant config
- **Hierarchical Config** (Phase 3) → Plugin marketplace configuration

## File Structure (Complete Phase 4)

```
api/app/
├── plugins/
│   ├── registry/
│   │   ├── service.py                    # Enhanced with marketplace
│   │   ├── validator.py                  # Signature validation
│   │   └── signature.py                  # NEW: GPG/RSA verification
│   ├── identity/                         # NEW: Enterprise identity
│   │   ├── ldap/
│   │   │   ├── plugin.py                 # LDAP/AD integration
│   │   │   └── manifest.json
│   │   ├── saml/
│   │   │   ├── plugin.py                 # SAML 2.0 SSO
│   │   │   └── manifest.json
│   │   └── oauth2/
│   │       ├── plugin.py                 # OAuth2/OIDC providers
│   │       └── manifest.json
│   └── storage/                          # NEW: Multi-cloud storage
│       ├── azure/
│       │   ├── plugin.py                 # Azure Blob Storage
│       │   └── manifest.json
│       └── gcs/
│           ├── plugin.py                 # Google Cloud Storage
│           └── manifest.json
├── events/
│   ├── bus.py                           # Enhanced with tenant isolation
│   ├── webhooks.py                      # Circuit breakers + compliance
│   └── delivery.py                      # Advanced delivery system
├── gateway/                             # NEW: Enterprise API gateway
│   ├── enterprise.py                    # Rate limiting + analytics
│   └── middleware.py                    # Gateway middleware
├── marketplace/                         # NEW: Plugin marketplace
│   ├── api.py                          # Marketplace API endpoints
│   └── ui_components.py                # Admin UI components
└── integrations/                       # NEW: Enterprise integrations
    ├── erp/
    │   ├── epic.py                     # Epic ERP integration
    │   └── salesforce.py               # Salesforce Health Cloud
    └── compliance/
        ├── hipaa_audit.py              # HIPAA audit extensions
        └── sox_compliance.py           # SOX compliance features

api/admin_ui/src/components/
├── PluginMarketplace.tsx               # NEW: Marketplace UI
├── EnterpriseIntegrations.tsx          # NEW: Enterprise connections
├── UsageAnalytics.tsx                  # NEW: API usage dashboard
└── ComplianceReporting.tsx             # NEW: Compliance reports

plugin-dev-kit/
├── python/
│   └── faxbot_plugin_dev/
│       ├── cli.py                      # Enhanced CLI tools
│       ├── dev_server.py               # Local development server
│       └── testing.py                  # Enhanced testing framework
├── templates/                          # NEW: Plugin templates
│   ├── transport/
│   ├── storage/
│   ├── identity/
│   └── integration/
└── docs/                              # NEW: Developer documentation
    ├── getting-started.md
    ├── api-reference.md
    └── deployment-guide.md
```

## Phase 4 Success Criteria

### 🎯 **Technical Deliverables**
✅ **Plugin Marketplace**: Secure discovery, installation, signature verification
✅ **Enterprise Identity**: LDAP, SAML, OAuth2 with trait-based permissions
✅ **Advanced Events**: Tenant-isolated event bus with compliance filtering
✅ **Multi-cloud Storage**: Azure, GCS with encryption and retention policies
✅ **Complete SDK**: CLI tools, local dev server, testing framework
✅ **API Gateway**: Per-tenant rate limiting, usage analytics, quotas

### 🔒 **Security & Compliance**
✅ **Plugin Signatures**: GPG/RSA verification prevents malicious plugins
✅ **Tenant Isolation**: Multi-tenant event bus and configuration
✅ **HIPAA Enhancement**: PHI-aware event filtering and audit trails
✅ **Circuit Breakers**: Webhook delivery reliability with failure isolation

### 📊 **Enterprise Features**
✅ **Usage Analytics**: Per-tenant API usage tracking and reporting
✅ **ERP Integrations**: Epic, Salesforce Health Cloud adapters
✅ **Advanced Webhooks**: Circuit breakers, retry logic, compliance filtering
✅ **Developer Portal**: Complete plugin development lifecycle support

### 🔧 **Infrastructure Compatibility**
✅ **All Existing Providers**: Phaxio, Sinch, SignalWire, SIP, FreeSWITCH preserved
✅ **All Current Storage**: Local, S3 enhanced with new cloud options
✅ **MCP Servers**: Node/Python enhanced with plugin-aware tools
✅ **Backward Compatibility**: All Phase 1-3 functionality preserved

## Timeline & Dependencies

**Week 1-2**: Plugin Marketplace & Registry Security
**Week 3-4**: Enterprise Identity Extensions (LDAP, SAML, OAuth2)
**Week 5-6**: Advanced Event System & Multi-Cloud Storage
**Week 7**: Complete Plugin SDK & API Gateway
**Week 8**: Integration Testing & Documentation

**Total Duration**: 8 weeks
**Team Size**: 2-3 developers + 1 DevOps engineer
**Key Milestone**: Enterprise-ready Faxbot platform with comprehensive plugin ecosystem

This Phase 4 plan builds upon ALL existing infrastructure while transforming Faxbot into an enterprise integration powerhouse. Every current service, plugin, and provider remains functional while gaining advanced enterprise capabilities.
