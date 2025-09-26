import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Literal, Any
from dataclasses import dataclass, field
from enum import Enum

from api.app.services.events import EventEmitter, EventType
from api.app.config.hierarchical_provider import HierarchicalConfigProvider

logger = logging.getLogger(__name__)

ProviderStatus = Literal['healthy', 'degraded', 'circuit_open', 'disabled']


@dataclass
class HealthCheck:
    """Provider health check result."""
    provider_id: str
    provider_type: str
    success: bool
    response_time_ms: float
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    checked_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CircuitBreakerState:
    """Circuit breaker state for a provider."""
    provider_id: str
    status: ProviderStatus = 'healthy'
    failure_count: int = 0
    last_failure_time: Optional[datetime] = None
    last_success_time: Optional[datetime] = None
    circuit_opened_at: Optional[datetime] = None
    next_retry_at: Optional[datetime] = None
    failure_threshold: int = 5
    recovery_timeout_seconds: int = 60
    health_check_interval_seconds: int = 300

    def should_allow_request(self) -> bool:
        """Check if requests should be allowed through circuit breaker."""
        now = datetime.utcnow()

        if self.status == 'healthy':
            return True
        elif self.status == 'degraded':
            return True  # Allow with warnings
        elif self.status == 'circuit_open':
            # Check if we should try to recover
            if self.next_retry_at and now >= self.next_retry_at:
                return True  # Try one request
            return False
        elif self.status == 'disabled':
            return False

        return False

    def record_success(self):
        """Record successful request."""
        self.last_success_time = datetime.utcnow()
        if self.status == 'circuit_open':
            # Circuit breaker recovery
            self.status = 'healthy'
            self.failure_count = 0
            self.circuit_opened_at = None
            self.next_retry_at = None
        elif self.status == 'degraded' and self.failure_count > 0:
            self.failure_count = max(0, self.failure_count - 1)
            if self.failure_count == 0:
                self.status = 'healthy'

    def record_failure(self, error: str):
        """Record failed request."""
        now = datetime.utcnow()
        self.last_failure_time = now
        self.failure_count += 1

        if self.failure_count >= self.failure_threshold:
            if self.status != 'circuit_open':
                self.status = 'circuit_open'
                self.circuit_opened_at = now
                self.next_retry_at = now + timedelta(seconds=self.recovery_timeout_seconds)
        elif self.failure_count >= self.failure_threshold // 2:
            if self.status == 'healthy':
                self.status = 'degraded'


class ProviderHealthMonitor:
    """Monitor provider health with circuit breaker functionality."""

    def __init__(
        self,
        plugin_manager=None,
        event_emitter: Optional[EventEmitter] = None,
        config_provider: Optional[HierarchicalConfigProvider] = None
    ):
        self.plugin_manager = plugin_manager
        self.event_emitter = event_emitter
        self.config_provider = config_provider
        self.circuit_states: Dict[str, CircuitBreakerState] = {}
        self.health_check_task: Optional[asyncio.Task] = None
        self.running = False

    async def start_monitoring(self):
        """Start background health monitoring."""
        if self.running:
            return

        self.running = True
        self.health_check_task = asyncio.create_task(self._health_check_loop())
        logger.info("Provider health monitoring started")

    async def stop_monitoring(self):
        """Stop background health monitoring."""
        self.running = False
        if self.health_check_task:
            self.health_check_task.cancel()
            try:
                await self.health_check_task
            except asyncio.CancelledError:
                pass
        logger.info("Provider health monitoring stopped")

    async def _health_check_loop(self):
        """Background loop for health checks."""
        while self.running:
            try:
                await self._perform_health_checks()

                # Get check interval from config
                interval = await self._get_health_check_interval()
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check loop error: {e}")
                await asyncio.sleep(60)  # Back off on error

    async def _perform_health_checks(self):
        """Perform health checks on all providers."""
        if not self.plugin_manager:
            return

        try:
            # Get transport plugins from plugin manager
            transport_plugins = getattr(self.plugin_manager, 'get_plugins_by_type', lambda x: {})('transport')

            if not transport_plugins:
                # Fallback: simulate providers for testing
                transport_plugins = {
                    'test': type('TestPlugin', (), {
                        'plugin_type': 'transport',
                        'check_health': lambda: {'ok': True, 'details': {'method': 'test'}}
                    })()
                }

            for provider_id, plugin in transport_plugins.items():
                try:
                    health_check = await self._check_provider_health(provider_id, plugin)
                    await self._update_circuit_breaker(health_check)

                except Exception as e:
                    logger.error(f"Provider health check error for {provider_id}: {e}")
        except Exception as e:
            logger.error(f"Failed to get transport plugins: {e}")

    async def _check_provider_health(self, provider_id: str, plugin) -> HealthCheck:
        """Check health of a specific provider."""
        start_time = datetime.utcnow()

        try:
            # Check if provider has health check method
            if hasattr(plugin, 'check_health'):
                if asyncio.iscoroutinefunction(plugin.check_health):
                    result = await plugin.check_health()
                else:
                    result = plugin.check_health()
                response_time = (datetime.utcnow() - start_time).total_seconds() * 1000

                return HealthCheck(
                    provider_id=provider_id,
                    provider_type=getattr(plugin, 'plugin_type', 'transport'),
                    success=result.get('ok', False),
                    response_time_ms=response_time,
                    details=result.get('details', {}),
                    error=result.get('error')
                )
            else:
                # No health check method - assume healthy if plugin loaded
                return HealthCheck(
                    provider_id=provider_id,
                    provider_type=getattr(plugin, 'plugin_type', 'transport'),
                    success=True,
                    response_time_ms=0,
                    details={'method': 'plugin_loaded_check'}
                )

        except Exception as e:
            response_time = (datetime.utcnow() - start_time).total_seconds() * 1000
            return HealthCheck(
                provider_id=provider_id,
                provider_type=getattr(plugin, 'plugin_type', 'transport'),
                success=False,
                response_time_ms=response_time,
                error=str(e)
            )

    async def _update_circuit_breaker(self, health_check: HealthCheck):
        """Update circuit breaker state based on health check."""
        provider_id = health_check.provider_id

        # Get or create circuit breaker state
        if provider_id not in self.circuit_states:
            self.circuit_states[provider_id] = CircuitBreakerState(
                provider_id=provider_id,
                failure_threshold=await self._get_circuit_breaker_threshold(provider_id),
                recovery_timeout_seconds=await self._get_circuit_breaker_timeout(provider_id)
            )

        circuit_state = self.circuit_states[provider_id]
        old_status = circuit_state.status

        if health_check.success:
            circuit_state.record_success()
        else:
            circuit_state.record_failure(health_check.error or 'Health check failed')

        # Emit event if status changed
        if circuit_state.status != old_status and self.event_emitter:
            await self.event_emitter.emit_event(
                EventType.PROVIDER_HEALTH_CHANGED,
                provider_id=provider_id,
                payload_meta={
                    'old_status': old_status,
                    'new_status': circuit_state.status,
                    'failure_count': circuit_state.failure_count,
                    'response_time_ms': health_check.response_time_ms
                }
            )

            logger.info(f"Provider {provider_id} status changed: {old_status} -> {circuit_state.status}")

    async def _get_health_check_interval(self) -> int:
        """Get health check interval from configuration."""
        try:
            if self.config_provider:
                system_ctx = {'user_id': 'system', 'groups': [], 'department': None, 'tenant_id': None}
                result = await self.config_provider.get_effective('provider.health_check_interval', system_ctx)
                return int(result.value) if result else 300
        except Exception:
            pass
        return 300  # 5 minutes default

    async def _get_circuit_breaker_threshold(self, provider_id: str) -> int:
        """Get circuit breaker threshold for provider."""
        try:
            if self.config_provider:
                system_ctx = {'user_id': 'system', 'groups': [], 'department': None, 'tenant_id': None}
                key = f'provider.{provider_id}.circuit_breaker_threshold'
                result = await self.config_provider.get_effective(key, system_ctx)
                return int(result.value) if result else 5
        except Exception:
            pass
        return 5  # Default threshold

    async def _get_circuit_breaker_timeout(self, provider_id: str) -> int:
        """Get circuit breaker recovery timeout for provider."""
        try:
            if self.config_provider:
                system_ctx = {'user_id': 'system', 'groups': [], 'department': None, 'tenant_id': None}
                key = f'provider.{provider_id}.circuit_breaker_timeout'
                result = await self.config_provider.get_effective(key, system_ctx)
                return int(result.value) if result else 60
        except Exception:
            pass
        return 60  # Default 1 minute

    def should_allow_request(self, provider_id: str) -> bool:
        """Check if provider should receive requests."""
        if provider_id not in self.circuit_states:
            return True  # No circuit breaker info, allow by default

        return self.circuit_states[provider_id].should_allow_request()

    def record_request_result(self, provider_id: str, success: bool, error: str = None):
        """Record result of provider request (for circuit breaker)."""
        if provider_id not in self.circuit_states:
            self.circuit_states[provider_id] = CircuitBreakerState(provider_id=provider_id)

        circuit_state = self.circuit_states[provider_id]
        old_status = circuit_state.status

        if success:
            circuit_state.record_success()
        else:
            circuit_state.record_failure(error or 'Request failed')

        # Emit event if status changed (async fire-and-forget)
        if circuit_state.status != old_status and self.event_emitter:
            asyncio.create_task(self.event_emitter.emit_event(
                EventType.PROVIDER_HEALTH_CHANGED,
                provider_id=provider_id,
                payload_meta={
                    'old_status': old_status,
                    'new_status': circuit_state.status,
                    'trigger': 'request_result'
                }
            ))

    async def get_provider_statuses(self) -> Dict[str, Dict]:
        """Get current status of all providers."""
        statuses = {}

        # Include all known providers from circuit states
        for provider_id, circuit_state in self.circuit_states.items():
            status_info = {
                'provider_id': provider_id,
                'provider_type': 'transport',
                'status': circuit_state.status,
                'failure_count': circuit_state.failure_count,
                'last_success': circuit_state.last_success_time.isoformat() if circuit_state.last_success_time else None,
                'last_failure': circuit_state.last_failure_time.isoformat() if circuit_state.last_failure_time else None,
            }

            if circuit_state.status == 'circuit_open':
                status_info['next_retry_at'] = circuit_state.next_retry_at.isoformat() if circuit_state.next_retry_at else None

            statuses[provider_id] = status_info

        return statuses

    async def manual_enable_provider(self, provider_id: str):
        """Manually enable a provider (admin action)."""
        if provider_id in self.circuit_states:
            old_status = self.circuit_states[provider_id].status
            self.circuit_states[provider_id].status = 'healthy'
            self.circuit_states[provider_id].failure_count = 0
            self.circuit_states[provider_id].circuit_opened_at = None
            self.circuit_states[provider_id].next_retry_at = None

            if self.event_emitter:
                await self.event_emitter.emit_event(
                    EventType.PROVIDER_ENABLED,
                    provider_id=provider_id,
                    payload_meta={
                        'old_status': old_status,
                        'new_status': 'healthy',
                        'trigger': 'manual_enable'
                    }
                )

            logger.info(f"Provider {provider_id} manually enabled")

    async def manual_disable_provider(self, provider_id: str):
        """Manually disable a provider (admin action)."""
        if provider_id not in self.circuit_states:
            self.circuit_states[provider_id] = CircuitBreakerState(provider_id=provider_id)

        old_status = self.circuit_states[provider_id].status
        self.circuit_states[provider_id].status = 'disabled'

        if self.event_emitter:
            await self.event_emitter.emit_event(
                EventType.PROVIDER_DISABLED,
                provider_id=provider_id,
                payload_meta={
                    'old_status': old_status,
                    'new_status': 'disabled',
                    'trigger': 'manual_disable'
                }
            )

        logger.info(f"Provider {provider_id} manually disabled")


