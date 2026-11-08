"""Torrent client adapters (JD-1b): version selection, uniform files, incremental changes."""
import json
import pathlib
import re
import unittest
import urllib.parse

from src.nextgen.acquisition.submission import ClientInvalid, ClientUnsupported
from src.nextgen.torrent_client.contract import ClientFile, TorrentChange
from src.nextgen.torrent_client.qbittorrent_versions import (
    Qbittorrent4, Qbittorrent5, Qbittorrent5Fetch, select_adapter)
from src.nextgen.tests.test_qb_transport import QBTransportTests, Response

HASH = 'a' * 40


class VersionAdapterTests(unittest.TestCase):
    def test_each_supported_version_gets_its_adapter(self):
        for major, api, adapter, start, stop, stopped, metadata in (
                (4, (2, 8, 19), Qbittorrent4, 'torrents/resume', 'torrents/pause', 'paused', 'guarded_export'),
                (4, (2, 9, 3), Qbittorrent4, 'torrents/resume', 'torrents/pause', 'paused', 'guarded_export'),
                (5, (2, 11, 2), Qbittorrent5, 'torrents/start', 'torrents/stop', 'stopped', 'guarded_export'),
                (5, (2, 11, 9), Qbittorrent5Fetch, 'torrents/start', 'torrents/stop', 'stopped', 'fetch')):
            with self.subTest(major=major, api=api):
                chosen = select_adapter(major, api)
                self.assertIs(type(chosen), adapter)
                self.assertEqual((chosen.start_endpoint, chosen.stop_endpoint, chosen.stopped_parameter,
                                  chosen.metadata_strategy), (start, stop, stopped, metadata))
                self.assertEqual(chosen.add_fields(True)[stopped], 'true')

    def test_unsupported_versions_are_refused(self):
        for major, api in ((3, (2, 8, 19)), (4, (2, 8, 13)), (5, (3, 0, 0)), (6, (2, 11, 9)), (5, (2, 11))):
            with self.subTest(major=major, api=api), self.assertRaises(ClientUnsupported):
                select_adapter(major, api)


class UniformReportTests(unittest.TestCase):
    client = QBTransportTests.client
    login_responses = QBTransportTests.login_responses

    def logged_in(self, responses, version=b'v5.0.0'):
        client = self.client(self.login_responses(version) + responses)
        client.login()
        return client

    def test_files_are_reported_with_absolute_paths_in_one_shape(self):
        for version in (b'v4.5.2', b'v5.0.0'):
            with self.subTest(version=version):
                info = [{'hash': HASH, 'save_path': '/srv/downloads/ad-x/', 'auto_tmm': False}]
                files = [{'index': 1, 'name': 'Show/Show - 02.mkv', 'size': 20, 'progress': 1, 'priority': 0},
                         {'index': 0, 'name': 'Show/Show - 01.mkv', 'size': 10, 'progress': 0.5, 'priority': 1}]
                client = self.logged_in([Response(json.dumps(info).encode()), Response(json.dumps(files).encode())],
                                        version)
                self.assertEqual(client.files(HASH), (
                    ClientFile(0, '/srv/downloads/ad-x/Show/Show - 01.mkv', 10, 0.5, True),
                    ClientFile(1, '/srv/downloads/ad-x/Show/Show - 02.mkv', 20, 1.0, False)))

    def test_files_of_an_absent_torrent_is_none(self):
        self.assertIsNone(self.logged_in([Response(b'[]')]).files(HASH))

    def test_files_refuses_paths_that_leave_the_save_path(self):
        for name in ('../escape.mkv', '/abs.mkv', 'a//b.mkv', 'a/./b.mkv'):
            with self.subTest(name=name):
                info = [{'hash': HASH, 'save_path': '/srv/downloads', 'auto_tmm': False}]
                files = [{'index': 0, 'name': name, 'size': 1, 'progress': 0, 'priority': 1}]
                client = self.logged_in([Response(json.dumps(info).encode()), Response(json.dumps(files).encode())])
                with self.assertRaises(ClientInvalid):
                    client.files(HASH)

    def test_changes_is_one_incremental_call_for_all_torrents(self):
        data = {'rid': 7, 'full_update': False,
                'torrents': {HASH: {'progress': 0.25, 'downloaded': 100, 'state': 'downloading', 'ratio': 0},
                             'b' * 64: {'progress': 1}},
                'torrents_removed': ['c' * 40]}
        client = self.logged_in([Response(json.dumps(data).encode())])
        changes = client.changes(3)
        self.assertEqual((changes.rid, changes.full, changes.removed), (7, False, ('c' * 40,)))
        self.assertEqual(changes.torrents, {HASH: TorrentChange(state='downloading', progress=0.25, downloaded=100)})
        request = self.requests[-1][0]
        self.assertEqual(urllib.parse.urlsplit(request.full_url).path, '/api/v2/sync/maindata')
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query), {'rid': ['3']})

    def test_changes_rejects_malformed_updates(self):
        for data in ({'rid': -1}, {'rid': 1, 'full_update': 'yes'},
                     {'rid': 1, 'torrents': {HASH: {'progress': 2}}},
                     {'rid': 1, 'torrents': {HASH: {'downloaded': -5}}}):
            with self.subTest(data=data), self.assertRaises(ClientInvalid):
                self.logged_in([Response(json.dumps(data).encode())]).changes()


class OnlyTheClientTalksToQbittorrentTests(unittest.TestCase):
    def test_no_other_module_calls_the_qbittorrent_web_api(self):
        # A new job or helper can't quietly start its own qBittorrent calls.
        root = pathlib.Path(__file__).resolve().parents[1]
        allowed = {'batch/qb_client.py'}                       # the batch session, until JD-4
        api = re.compile(r"""['"](?:/api/v2/?|torrents/[a-zA-Z]+|sync/maindata|auth/login|app/(?:version|webapiVersion))['"]""")
        found = []
        for path in root.rglob('*.py'):
            relative = path.relative_to(root).as_posix()
            if relative.startswith(('tests/', 'torrent_client/')) or relative in allowed:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if api.search(line):
                    found.append(f'{relative}:{number}: {line.strip()}')
        self.assertEqual(found, [])


if __name__ == '__main__':
    unittest.main()
