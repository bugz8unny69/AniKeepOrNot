"""Keep destination recovery uses the same worker retry for both scopes."""
import unittest
from types import SimpleNamespace
from threading import Event

from src.anime.post_keep_cleanup import registration as single_cleanup
from src.anime.jobs.cleanup_batch_kept_media_job import registration as batch_cleanup


class UnavailableKeepDestinationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_destination_retries_for_single_and_batch(self):
        class MissingFile:
            def __init__(self,configuration,secrets):
                pass
            def run(self,claim,keep,cancelled):
                raise FileNotFoundError('Sonarr NAS file temporarily missing')

        context=SimpleNamespace(
            data=lambda:{'keep_job_id':17},
            configuration=object(),cancelled=Event(),
            claim=SimpleNamespace(job_id=23),
        )
        for handler in (single_cleanup,batch_cleanup):
            with self.subTest(kind=handler):
                outcome=await handler(
                    object(),service_factory=MissingFile).execute(context)
                self.assertEqual((outcome.kind,outcome.reason_code),
                                 ('retry','POST_KEEP_CLEANUP_UNAVAILABLE'))
