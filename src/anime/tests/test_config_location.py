"""Command-line tools default to the RUNNING branch's settings folder."""
import pathlib
import tempfile
import unittest

from src.anime.config_location import FALLBACK, default_config_dir, from_unit


class ConfigLocationTests(unittest.TestCase):
    def rendered_unit(self, config_dir):
        # The unit exactly as the installer writes it.
        from src.anime.configuration_schema import ConfigurationSnapshot
        from src.anime.install_support import units
        rendered = units(ConfigurationSnapshot(1, {}), '/opt/anime-discovery', config_dir,
                         '/var/lib/anime-discovery', 'anime-discovery', None)
        folder = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in folder.iterdir()] and folder.rmdir())
        path = folder / 'anime-discovery-worker.service'
        path.write_text(rendered['anime-discovery-worker.service'])
        return path

    def test_the_installed_worker_unit_names_the_running_branchs_folder(self):
        unit = self.rendered_unit('/etc/anime-discovery/branches/refactor')
        self.assertEqual(from_unit(unit), '/etc/anime-discovery/branches/refactor')
        self.assertEqual(default_config_dir(environ={}, units=(unit,)), '/etc/anime-discovery/branches/refactor')

    def test_folders_with_spaces_and_percent_signs_survive_the_unit_quoting(self):
        unit = self.rendered_unit('/etc/anime discovery/branches/50%')
        self.assertEqual(from_unit(unit), '/etc/anime discovery/branches/50%')

    def test_the_environment_wins_and_no_unit_falls_back(self):
        unit = self.rendered_unit('/etc/anime-discovery/branches/refactor')
        self.assertEqual(default_config_dir(environ={'ANIME_DISCOVERY_CONFIG_DIR': '/srv/conf'}, units=(unit,)), '/srv/conf')
        self.assertEqual(default_config_dir(environ={}, units=(pathlib.Path('/nonexistent/unit'),)), FALLBACK)

    def test_every_tool_uses_the_shared_default(self):
        import ast
        root = pathlib.Path(__file__).resolve().parents[3]
        hard_coded = []
        for path in [*(root / 'src/anime').rglob('*.py'), *(root / 'fixes').glob('*.py')]:
            if 'tests' in path.parts or path.name == 'config_location.py':
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.keyword) and node.arg == 'default':
                    value = node.value.args[0] if isinstance(node.value, ast.Call) and node.value.args else node.value
                    if isinstance(value, ast.Constant) and value.value == '/etc/anime-discovery':
                        hard_coded.append(f'{path.relative_to(root)}:{node.value.lineno}')
        self.assertEqual(hard_coded, [])


if __name__ == '__main__':
    unittest.main()
