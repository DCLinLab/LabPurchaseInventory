import tempfile
from pathlib import Path
from threading import Timer
import unittest

from runtime_lock import InstanceLock


class LockTests(unittest.TestCase):
    def test_worker_waits_for_another_writer_and_timeout_still_works(self):
        with tempfile.TemporaryDirectory() as temp:
            first = InstanceLock(Path(temp) / 'test.lock')
            second = InstanceLock(first.path)
            first.acquire()
            with self.assertRaises(ValueError): second.acquire(timeout=0.02)
            timer = Timer(0.05, first.release); timer.start()
            try:
                second.acquire(timeout=2)
                self.assertIsNotNone(second.handle)
            finally:
                timer.join(); first.release(); second.release()
