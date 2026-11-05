"""`adm services check`: table, JSON, exit codes and plugin adapters."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.nextgen.configuration import ConfigurationStore
from src.nextgen.services import DISABLED, DOWN, UP, ServiceAdapter, ServiceRegistry
from src.nextgen.services.check import check, main


class Plugin(ServiceAdapter):
    """Stands in for a service added later as a plugin."""
    def __init__(self, name, label, status, detail='', network_probe=True):
        self.name, self.label, self.status, self.detail = name, label, status, detail
        self.network_probe = network_probe

    def probe(self, configuration, secrets, *, now_ms=0):
        return self.result(self.status, self.detail, now_ms=now_ms)


class ServicesCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        ConfigurationStore(self.root).initialize({'storage': {'database_path': str(self.root / 'db.sqlite')}})

    def run_main(self, registry, *args):
        lines = []
        with patch('sys.stderr', io.StringIO()) as err:
            code = main(['--config-dir', str(self.root), 'check', *args], out=lines.append, registry=registry)
        return code, '\n'.join(lines), err.getvalue()

    def test_all_usable_exits_zero_and_prints_a_table(self):
        registry = ServiceRegistry([Plugin('alpha', 'Alpha', UP, 'version 2'),
                                    Plugin('beta', 'Beta', DISABLED),
                                    Plugin('gamma', 'Gamma', UP, network_probe=False)])
        code, text, _ = self.run_main(registry)
        self.assertEqual(code, 0)
        self.assertIn('Alpha    OK      Alpha is available: version 2', text)
        self.assertIn('Beta is turned off (optional).', text)
        self.assertIn('Gamma is available. (from cache)', text)

    def test_any_problem_exits_one_and_names_the_service(self):
        registry = ServiceRegistry([Plugin('alpha', 'Alpha', UP),
                                    Plugin('prowlarr', 'Prowlarr', DOWN, 'HTTP 503 at http://p:9696')])
        code, text, _ = self.run_main(registry)
        self.assertEqual(code, 1)
        self.assertIn('Prowlarr is unavailable: HTTP 503 at http://p:9696', text)

    def test_json_and_service_selection(self):
        registry = ServiceRegistry([Plugin('alpha', 'Alpha', UP), Plugin('beta', 'Beta', DOWN, 'gone')])
        code, text, _ = self.run_main(registry, '--json', '--service', 'beta')
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text), [{'service': 'beta', 'label': 'Beta', 'status': 'DOWN',
                                            'message': 'Beta is unavailable: gone', 'detail': 'gone',
                                            'from_cache': False}])

    def test_unknown_service_and_unreadable_settings_exit_two(self):
        registry = ServiceRegistry([Plugin('alpha', 'Alpha', UP)])
        code, _, err = self.run_main(registry, '--service', 'nope')
        self.assertEqual(code, 2)
        self.assertIn('unknown service: nope', err)
        lines = []
        with patch('sys.stderr', io.StringIO()) as err:
            code = main(['--config-dir', str(self.root / 'missing'), 'check'], out=lines.append,
                        registry=registry)
        self.assertEqual(code, 2)
        self.assertIn('could not be read', err.getvalue())

    def test_check_runs_each_selected_probe_once(self):
        calls = []

        class Counting(Plugin):
            def probe(self, configuration, secrets, *, now_ms=0):
                calls.append(self.name)
                return super().probe(configuration, secrets, now_ms=now_ms)

        registry = ServiceRegistry([Counting('a', 'A', UP), Counting('b', 'B', UP)])
        states = check(None, None, registry=registry)
        self.assertEqual(([s.service for s in states], calls), (['a', 'b'], ['a', 'b']))

    def test_adm_routes_services(self):
        from src.nextgen.api import adm
        with patch('sys.argv', ['adm', 'services', '--config-dir', str(self.root), 'check', '--json']), \
                patch('src.nextgen.services.check.main', return_value=0) as routed, \
                self.assertRaises(SystemExit) as done:
            adm.main()
        self.assertEqual(done.exception.code, 0)
        routed.assert_called_once_with(['--config-dir', str(self.root), 'check', '--json'])


if __name__ == '__main__':
    unittest.main()
