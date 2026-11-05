"""Service health foundation: results, registry, plugins and the health monitor."""
import unittest

from src.nextgen.providers.errors import ProviderDeferred, ProviderUnavailable
from src.nextgen.services import (
    AUTH_FAILED, DISABLED, DOWN, NOT_CONFIGURED, UNKNOWN, UP, ProbeResult, ServiceAdapter,
    ServiceHealth, ServiceRegistry, default_registry,
)


class Clock:
    def __init__(self, now=1_000_000):
        self.now = now

    def __call__(self):
        return self.now


class FakeAdapter(ServiceAdapter):
    """A plugin-style adapter: proves new services need no framework changes."""
    name = 'fake'
    label = 'Fake Service'

    def __init__(self, status=UP, detail=''):
        self.status, self.detail, self.calls = status, detail, 0

    def probe(self, configuration, secrets, *, now_ms=0):
        self.calls += 1
        return self.result(self.status, self.detail, now_ms=now_ms)

    def classify(self, exc):
        return AUTH_FAILED if isinstance(exc, PermissionError) else (
            DOWN if isinstance(exc, ConnectionError) else None)


class BrokenAdapter(ServiceAdapter):
    name = 'broken'
    label = 'Broken'

    def probe(self, configuration, secrets, *, now_ms=0):
        raise KeyError('bug in the adapter')


class ProbeResultTests(unittest.TestCase):
    def test_messages_name_the_service_and_the_problem(self):
        self.assertEqual(ProbeResult('prowlarr', DOWN, 'HTTP 503 at http://p:9696').message('Prowlarr'),
                         'Prowlarr is unavailable: HTTP 503 at http://p:9696')
        self.assertEqual(ProbeResult('prowlarr', UP).message('Prowlarr'), 'Prowlarr is available.')
        self.assertEqual(ProbeResult('jellyfin', DISABLED).message('Jellyfin'),
                         'Jellyfin is turned off (optional).')
        self.assertEqual(ProbeResult('sonarr', AUTH_FAILED, 'HTTP 401').message('Sonarr'),
                         'Sonarr rejected the login or API key: HTTP 401')

    def test_invalid_results_are_rejected(self):
        for args in (('Bad Name', UP), ('ok', 'MAYBE'), ('ok', UP, 'x' * 501), ('ok', UP, '', -1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                ProbeResult(*args)

    def test_usable_statuses_do_not_block_work(self):
        self.assertTrue(all(ProbeResult('s', s).usable for s in (UP, DISABLED, UNKNOWN)))
        self.assertFalse(any(ProbeResult('s', s).usable for s in (DOWN, NOT_CONFIGURED, AUTH_FAILED)))


class RegistryTests(unittest.TestCase):
    def test_default_registry_covers_every_service(self):
        registry = default_registry()
        self.assertEqual(registry.names(), ('qbittorrent', 'prowlarr', 'sonarr', 'jellyfin', 'shoko',
                                            'anidb', 'anime_lists', 'tvdb'))
        # AniDB, Anime-Lists and TVDB are never contacted just to check health.
        self.assertEqual({a.name for a in registry if not a.network_probe},
                         {'anidb', 'anime_lists', 'tvdb'})
        self.assertEqual({a.name for a in registry if a.optional}, {'jellyfin', 'shoko'})

    def test_plugins_register_and_duplicates_are_rejected(self):
        registry = ServiceRegistry()
        registry.register(FakeAdapter())
        self.assertIn('fake', registry)
        with self.assertRaises(ValueError):
            registry.register(FakeAdapter())
        with self.assertRaises(ValueError):
            registry.get('missing')
        bad = FakeAdapter()
        bad.name = 'Not Valid'
        with self.assertRaises(ValueError):
            ServiceRegistry([bad])
        with self.assertRaises(ValueError):
            ServiceRegistry([object()])


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.adapter = FakeAdapter()
        self.health = ServiceHealth(ServiceRegistry([self.adapter, BrokenAdapter()]), clock=self.clock,
                                    recheck_ms=60000, min_backoff_ms=15000, max_backoff_ms=120000)

    def test_unknown_until_checked_and_unknown_does_not_block(self):
        self.assertEqual(self.health.state('fake').status, UNKNOWN)
        self.assertTrue(self.health.is_available('fake'))

    def test_each_change_is_reported_once(self):
        self.adapter.status = DOWN
        first = self.health.probe('fake', None, None)
        self.assertEqual((first.old, first.new), (UNKNOWN, DOWN))
        self.clock.now += 20000
        self.assertIsNone(self.health.probe('fake', None, None))  # Still down: no repeat.
        self.adapter.status = UP
        self.clock.now += 60000
        back = self.health.probe('fake', None, None)
        self.assertEqual((back.old, back.new), (DOWN, UP))
        self.assertEqual(back.state.since_ms, self.clock.now)

    def test_backoff_grows_while_down_and_is_capped(self):
        self.adapter.status = DOWN
        delays = []
        for _ in range(5):
            self.health.probe('fake', None, None)
            state = self.health.state('fake')
            delays.append(state.next_probe_ms - state.checked_at_ms)
            self.clock.now = state.next_probe_ms
        self.assertEqual(delays, [15000, 30000, 60000, 120000, 120000])
        self.adapter.status = UP
        self.health.probe('fake', None, None)
        state = self.health.state('fake')
        self.assertEqual((state.failures, state.next_probe_ms - state.checked_at_ms), (0, 60000))

    def test_due_follows_the_schedule(self):
        self.assertTrue(self.health.due('fake'))
        self.health.probe('fake', None, None)
        self.assertFalse(self.health.due('fake'))
        self.clock.now += 60000
        self.assertTrue(self.health.due('fake'))

    def test_real_request_failure_marks_down_without_a_probe(self):
        change = self.health.mark_failed('fake', ConnectionError('refused'))
        self.assertEqual((change.new, self.adapter.calls), (DOWN, 0))
        self.assertFalse(self.health.is_available('fake'))
        self.assertEqual(self.health.unavailable(('fake', 'broken')), ('fake',))
        self.assertEqual(self.health.mark_failed('fake', PermissionError('401')).new, AUTH_FAILED)
        self.assertEqual(self.health.mark_succeeded('fake').new, UP)
        self.assertIsNone(self.health.mark_succeeded('fake'))

    def test_unrecognised_errors_mark_down_and_planned_waits_do_not(self):
        from src.nextgen.services.adapters.metadata import AnidbAdapter
        health = ServiceHealth(ServiceRegistry([AnidbAdapter()]), clock=self.clock)
        self.assertIsNone(health.mark_failed('anidb', ProviderDeferred(60000)))
        self.assertEqual(health.state('anidb').status, UNKNOWN)
        self.assertEqual(health.mark_failed('anidb', ProviderUnavailable()).new, DOWN)
        self.assertEqual(self.health.mark_failed('fake', ValueError('odd')).new, DOWN)

    def test_a_buggy_adapter_cannot_break_the_caller(self):
        change = self.health.probe('broken', None, None)
        self.assertEqual(change.new, DOWN)
        self.assertIn('KeyError', self.health.state('broken').detail)

    def test_probe_due_checks_only_what_is_due(self):
        changes = self.health.probe_due(None, None)
        self.assertEqual({c.service for c in changes}, {'fake', 'broken'})
        self.assertEqual(self.health.probe_due(None, None), ())
        self.assertEqual(self.adapter.calls, 1)

    def test_concurrent_callers_do_not_probe_the_same_service_twice(self):
        import threading
        barrier = threading.Barrier(4)

        def call():
            barrier.wait()
            self.health.probe_due(None, None, services=('fake',))
        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(self.adapter.calls, 1)

    def test_recheck_all_and_mark_reason(self):
        self.health.probe('fake', None, None)
        self.assertFalse(self.health.due('fake'))
        self.health.recheck_all()
        self.assertTrue(self.health.due('fake'))
        self.adapter.reasons = {'FAKE_DOWN': DOWN}
        self.assertEqual(self.health.mark_reason('fake', 'FAKE_DOWN', detail='x').new, DOWN)
        self.assertIsNone(self.health.mark_reason('fake', 'SOMETHING_ELSE'))

    def test_invalid_timing_is_rejected(self):
        with self.assertRaises(ValueError):
            ServiceHealth(ServiceRegistry(), min_backoff_ms=500)
        with self.assertRaises(ValueError):
            ServiceHealth(ServiceRegistry(), min_backoff_ms=60000, max_backoff_ms=1000)


if __name__ == '__main__':
    unittest.main()
