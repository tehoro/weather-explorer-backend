import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests_store as requests
import scene_job


class StabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_scene_worker_yields_and_lowers_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy(Path(scene_job.__file__), root / 'scene_job.py')
            (root / 'scenes.py').write_text(
                'import os,time\n'
                'def build(path):\n'
                '    time.sleep(0.2)\n'
                '    path.write_text(str(os.getpriority(os.PRIO_PROCESS, 0)))\n')
            out = root / 'result'
            process = await asyncio.create_subprocess_exec(sys.executable, str(root / 'scene_job.py'), str(out))
            ticks = 0
            while process.returncode is None:
                ticks += 1
                await asyncio.sleep(.01)
            self.assertEqual(process.returncode, 0)
            self.assertGreaterEqual(int(out.read_text()), 15)
            self.assertGreater(ticks, 5)

    async def test_scene_worker_failure_and_cancellation(self):
        process = AsyncMock()
        process.wait.return_value = 1
        with patch('scene_job.asyncio.create_subprocess_exec', return_value=process):
            with self.assertRaises(RuntimeError):
                await scene_job.build(Path('/tmp/unused'))
        from unittest.mock import Mock
        process = Mock(returncode=None)
        process.wait = AsyncMock(side_effect=[asyncio.CancelledError(), 0])
        with patch('scene_job.asyncio.create_subprocess_exec', return_value=process):
            with self.assertRaises(asyncio.CancelledError):
                await scene_job.build(Path('/tmp/unused'))
        process.terminate.assert_called_once()

    async def test_slow_storage_does_not_block_loop_and_submission_queues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = asyncio.Queue()
            with patch.multiple(requests, DIR=root, ACTIVE=root/'active.json', _queue=queue,
                                _loop=asyncio.get_running_loop(), _by_device={}), \
                 patch.object(requests.store, 'save_requests', side_effect=lambda: time.sleep(.15)):
                # defaultdict is the production per-device counter.
                from collections import defaultdict, deque
                requests._by_device = defaultdict(deque)
                task = asyncio.create_task(asyncio.to_thread(requests.submit, 'Storm over Italy', 'test'))
                await asyncio.sleep(.02)
                self.assertFalse(task.done())
                accepted, _ = await task
                self.assertTrue(accepted)
                rid = await asyncio.wait_for(queue.get(), 1)
                entries = json.loads((root/'active.json').read_text())
                self.assertEqual(entries[0]['id'], rid)
                self.assertEqual(entries[0]['status'], 'preparing')

    async def test_request_worker_publishes_scene_enriched_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = {'id':'abc', 'status':'preparing'}
            (root/'active.json').write_text(json.dumps([entry]))
            async def research(topic, draft, out, log):
                out.write_text(json.dumps({'items':[{'id':'test','scenes':[]}]}))
                return 'RESULT: published'
            async def build(out, tag):
                feed = json.loads(out.read_text())
                feed['items'][0]['scenes'] = [{'data':{'fill':tag+'/fill.json.gz'}}]
                out.write_text(json.dumps(feed))
            with patch.multiple(requests, DIR=root, ACTIVE=root/'active.json'), \
                 patch.object(requests.agent, 'run_request', side_effect=research), \
                 patch.object(requests.scene_job, 'build', side_effect=build), \
                 patch.object(requests.store, 'save_requests'), \
                 patch.object(requests.store, 'save_request_file') as save:
                await requests._research({**entry, 'topic':'Storm over Italy'})
                feed = json.loads((root/'abc.json').read_text())
                self.assertEqual(feed['items'][0]['id'], 'req-test')
                self.assertIn('fill', feed['items'][0]['scenes'][0]['data'])
                self.assertEqual(json.loads((root/'active.json').read_text())[0]['status'], 'ready')
                save.assert_called_once()


if __name__ == '__main__':
    unittest.main()
