import asyncio
import threading

from quantlab.engine import Platform
from quantlab.service.runtime import ServiceRuntime


def test_real_platform_emergency_does_not_wait_for_research_lock(tmp_path):
    platform = Platform(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def hold_research():
        with platform.lock:
            entered.set()
            assert release.wait(5)

    worker = threading.Thread(target=hold_research)
    worker.start()
    assert entered.wait(2)
    try:
        result = platform.emergency_halt('operator stop')
        assert result['halted'] is True
        assert platform.broker.orders() == []
        assert platform.broker.fills() == []
    finally:
        release.set()
        worker.join(5)
        platform.close()
    restored = Platform(tmp_path)
    assert restored.risk.status()['halted'] is True
    restored.close()


def test_stop_overtakes_active_research_and_is_audited(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class SlowPlatform(Platform):
        def run_demo(self, **parameters):
            with self.lock:
                entered.set()
                assert release.wait(5)
                return {'halted_before_execution': self.risk.status()['halted']}

    async def scenario():
        runtime = ServiceRuntime(tmp_path, SlowPlatform)
        runtime.audit.begin('research', 'POST', '/api/runs')
        runtime.audit.begin('stop', 'POST', '/api/risk/halt')
        research = asyncio.create_task(runtime.mutate('research', 'run_demo', {}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            result = await asyncio.wait_for(runtime.mutate('stop', 'set_halt', {
                'halted': True, 'reason': 'urgent stop',
            }), 2)
            assert result['halted'] is True
            assert not research.done()
            assert runtime.health()['service']['last_control']['status'] == 'completed'
        finally:
            release.set()
        assert (await research)['halted_before_execution'] is True
        entries = runtime.audit.entries()
        assert next(row for row in entries if row['request_id'] == 'stop')['outcome'] == 'completed'
        runtime.close()

    asyncio.run(scenario())
