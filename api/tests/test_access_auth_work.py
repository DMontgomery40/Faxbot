"""Internal worker admission/lifetime checks; no HTTP or user-flow acceptance."""
import asyncio
import importlib
import threading

import pytest

try:
    W = importlib.import_module('api.app.access.auth_work')
except ModuleNotFoundError:
    W = None


def test_module_exists():
    assert W is not None, 'Bounded authentication worker missing'


def test_four_workers_refuse_extra_work_without_queuing_and_release():
    assert W is not None
    async def scenario():
        gate = W.AuthenticationWork()
        release = threading.Event()
        entered = [threading.Event() for _ in range(4)]
        calls = []
        def work(index):
            calls.append(index)
            entered[index].set()
            assert release.wait(5)
            return index
        tasks = [asyncio.create_task(gate.run(lambda i=i: work(i))) for i in range(4)]
        try:
            assert all(await asyncio.gather(*(asyncio.to_thread(e.wait, 5) for e in entered)))
            with pytest.raises(W.AuthenticationBusyError):
                await gate.run(lambda: calls.append(99))
            assert sorted(calls) == list(range(4))
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
        assert results == list(range(4))
        assert await gate.run(lambda: 42) == 42
    asyncio.run(scenario())


@pytest.mark.parametrize('worker_failure', [False, True])
def test_repeated_cancellation_keeps_permit_until_whole_worker_ends(worker_failure):
    assert W is not None
    async def scenario():
        gate = W.AuthenticationWork()
        release = threading.Event()
        entered = [threading.Event() for _ in range(4)]
        finished = threading.Event()
        def work(index):
            entered[index].set()
            assert release.wait(5)
            if index == 0:
                finished.set()
                if worker_failure:
                    raise RuntimeError('synthetic private worker detail')
            return index
        tasks = [asyncio.create_task(gate.run(lambda i=i: work(i))) for i in range(4)]
        try:
            assert all(await asyncio.gather(*(asyncio.to_thread(e.wait, 5) for e in entered)))
            for _ in range(3):
                tasks[0].cancel()
                await asyncio.sleep(0)
                assert not tasks[0].done()
                with pytest.raises(W.AuthenticationBusyError):
                    await gate.run(lambda: None)
            assert not finished.is_set()
        finally:
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert finished.is_set()
        assert await gate.run(lambda: 42) == 42
    asyncio.run(scenario())


def test_worker_exception_releases_permit_and_does_not_block_event_loop():
    assert W is not None
    async def scenario():
        gate = W.AuthenticationWork()
        main_thread = threading.get_ident()
        def fail():
            assert threading.get_ident() != main_thread
            raise ValueError('internal failure')
        for _ in range(6):
            with pytest.raises(ValueError, match='internal failure'):
                await gate.run(fail)
        assert await gate.run(threading.get_ident) != main_thread
    asyncio.run(scenario())


def test_shutdown_cancelling_child_tasks_cannot_free_running_thread_permits():
    assert W is not None
    async def scenario():
        gate = W.AuthenticationWork()
        release = threading.Event()
        entered = [threading.Event() for _ in range(4)]
        def work(index):
            entered[index].set()
            assert release.wait(5)
        tasks = [asyncio.create_task(gate.run(lambda i=i: work(i))) for i in range(4)]
        try:
            assert all(await asyncio.gather(*(asyncio.to_thread(e.wait, 5) for e in entered)))
            # asyncio shutdown cancels child Tasks too, beyond cancellation of
            # just the protected run() caller. Thread lifetime must survive it.
            children = asyncio.all_tasks() - {asyncio.current_task()}
            for task in children:
                task.cancel()
            for _ in range(4):
                await asyncio.sleep(0)
            with pytest.raises(W.AuthenticationBusyError):
                await gate.run(lambda: None)
            assert all(not task.done() for task in tasks)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(scenario())


def test_worker_preserves_callers_context_variables():
    assert W is not None
    from contextvars import ContextVar
    value = ContextVar('synthetic_auth_context', default='missing')
    async def scenario():
        value.set('captured')
        assert await W.AuthenticationWork().run(value.get) == 'captured'
    asyncio.run(scenario())
