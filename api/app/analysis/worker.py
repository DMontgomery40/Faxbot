"""Periodic claim and bounded analysis, using the current active configuration."""
import asyncio
import logging
from ..config_runtime import run_lifecycle_step
from .agent import analyze, AnalysisError
from .evidence import OperationalEvidence
from .store import configured

FIELDS = ('analysis_enabled', 'analysis_provider', 'analysis_base_url', 'analysis_model', 'analysis_api_key')


class AnalysisWorker:
    def __init__(self, store, values_source, *, runner=analyze):
        self.store, self.values_source, self.runner = store, values_source, runner

    async def step(self):
        values = await run_lifecycle_step(self.values_source)
        run = await run_lifecycle_step(lambda: self.store.claim(values))
        if run is None:
            return False

        async def allowed():
            current = await run_lifecycle_step(self.values_source)
            return (current.analysis_enabled and configured(current)
                    and all(getattr(current, field) == getattr(values, field) for field in FIELDS))

        try:
            source = await run_lifecycle_step(lambda: OperationalEvidence(self.store.engine))
            result = await self.runner(values, source, allowed=allowed)
            if not await allowed():
                raise AnalysisError('Analysis was disabled or its configuration changed. Refresh with the current settings.')
        except asyncio.CancelledError:
            # Cancellation leaves the lease durable. Another worker marks interruption
            # after expiry; it cannot duplicate a provider request during shutdown.
            raise
        except AnalysisError as error:
            result = {'error': str(error)}
        except Exception:
            result = {'error': 'Operational analysis could not complete. Refresh to try again.'}
        await run_lifecycle_step(lambda: self.store.finish(run, **result))
        return False

    async def run(self):
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning('Operational analysis is temporarily unavailable.')
            await asyncio.sleep(5)
