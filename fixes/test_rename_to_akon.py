"""Tests and temporary-root rehearsal for fixes/rename_to_akon.py (RN-2, RN-3, RN-6)."""
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from fixes.rename_to_akon import rename_to_akon
from src.anime.configuration import ConfigurationStore
from src.anime.secrets import SecretStore
from src.anime.series_job.store import add_torrent, get_series, wake
from src.anime.storage.sqlite import connect, initialize


class RenameToAkonTests(unittest.TestCase):
    def _build_prod_layout(self, root: Path):
        etc = root / "etc/anime-discovery"
        dev_cfg = etc / "branches/develop"
        ref_cfg = etc / "branches/refactor"
        for d in (etc, dev_cfg, ref_cfg):
            d.mkdir(parents=True)

        ConfigurationStore(etc).initialize({"storage": {"database_path": "/var/lib/anime-discovery/anime-discovery.sqlite"}})
        ConfigurationStore(ref_cfg).initialize({"storage": {"database_path": "/var/lib/anime-discovery/anime-discovery-refactor.sqlite"}})
        ConfigurationStore(dev_cfg).initialize({
            "storage": {
                "database_path": "/var/lib/anime-discovery/anime-discovery-develop.sqlite",
                "download_root": "/mnt/media/Torrents/anime",
            },
            "qbittorrent": {"base_url": "http://qb.test", "category": "anime-discovery"},
        })
        secrets = SecretStore(dev_cfg)
        secrets.initialize("tvdb-key")
        secrets.update_qbittorrent_credentials("admin", "pw", expected_revision=1)

        var = root / "var/lib/anime-discovery"
        (var / "installer").mkdir(parents=True)
        (var / "anime-discovery-develop-files/metainfo").mkdir(parents=True)
        (var / "anime-discovery.sqlite").write_bytes(b"nextgen-db")
        (var / "anime-discovery-refactor.sqlite").write_bytes(b"refactor-db")

        db_path = var / "anime-discovery-develop.sqlite"
        db = connect(db_path)
        initialize(db)
        wake(db, 101, "Test Series", now=1000)
        add_torrent(db, 101, {
            "infohash": "a" * 40,
            "release_name": "[G] Test Series - 01",
            "step": "download",
            "state": "working",
            "qb": {"marker": "1" * 32, "added": "sent"},
        }, now=1000)
        db.close()

        installer_doc = {
            "branches": {
                "NextGen": {"config_dir": "/etc/anime-discovery"},
                "develop": {"config_dir": "/etc/anime-discovery/branches/develop"},
                "refactor": {"config_dir": "/etc/anime-discovery/branches/refactor"},
            },
            "config_base": "/etc/anime-discovery",
            "config_dir": "/etc/anime-discovery/branches/develop",
            "destination": "/usr/local/bin/akon-install",
            "origin": "http://127.0.0.1:8765",
            "python": "python3",
            "ref": "develop",
            "root": "/opt/anime-discovery",
            "source": "git@github.com:bugz8unny69/anime-discovery.git",
            "state_dir": "/var/lib/anime-discovery",
            "unit_dir": "/etc/systemd/system",
            "user": "anime-discovery",
        }
        (var / "installer/installer.json").write_text(json.dumps(installer_doc))

        bin_dir = root / "usr/local/bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "adm").write_text("#!/bin/sh\nANIME_DISCOVERY_CONFIG_DIR=/etc/anime-discovery/branches/develop\n")
        (bin_dir / "anime-discovery-admin").write_text("#!/bin/sh\n")

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_prod_layout(root)
            cmds = []
            def fake_exec(cmd, *, check=True):
                cmds.append(cmd)
                if cmd[:2] == ["systemctl", "is-active"]:
                    return subprocess.CompletedProcess(cmd, 3, "", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            logs = []
            plan = rename_to_akon(prefix=root, apply=False, execute=fake_exec, out=logs.append)
            self.assertFalse(plan["apply"])
            self.assertTrue((root / "etc/anime-discovery").exists())
            self.assertFalse((root / "etc/akon").exists())
            self.assertFalse(any(c[0] in ("usermod", "groupmod") for c in cmds))

    def test_full_rename_rehearsal_in_temp_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_prod_layout(root)
            cmds = []
            def fake_exec(cmd, *, check=True):
                cmds.append(cmd)
                if cmd[:2] == ["systemctl", "is-active"]:
                    return subprocess.CompletedProcess(cmd, 3, "", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            fake_qb = mock.Mock()
            fake_qb.rename_legacy_torrents.return_value = {
                "renamed": [{"hash": "a" * 40, "marker": "1" * 32, "name": "[G] Test Series - 01"}],
                "skipped": [],
                "removed_old_category": True,
            }
            logs = []
            plan = rename_to_akon(
                prefix=root,
                apply=True,
                execute=fake_exec,
                qb_client_factory=lambda opts, creds: fake_qb,
                out=logs.append,
            )

            self.assertEqual(["usermod", "-l", "akon", "anime-discovery"], cmds[4])
            self.assertEqual(["groupmod", "-n", "akon", "anime-discovery"], cmds[5])
            self.assertEqual(["usermod", "-d", "/var/lib/akon", "akon"], cmds[6])    # Its home moves with the data.
            self.assertFalse((root / "etc/anime-discovery").exists())
            self.assertTrue((root / "etc/akon").exists())
            self.assertFalse((root / "var/lib/anime-discovery").exists())
            self.assertTrue((root / "var/lib/akon").exists())
            self.assertTrue((root / "var/lib/akon/akon.sqlite").is_file())
            self.assertTrue((root / "var/lib/akon/akon-files/metainfo").is_dir())
            # Other branches' DBs left alone (RN-0 Q2)
            self.assertTrue((root / "var/lib/akon/anime-discovery.sqlite").is_file())
            self.assertTrue((root / "var/lib/akon/anime-discovery-refactor.sqlite").is_file())

            # Settings rewritten via ConfigurationStore
            dev_snap = ConfigurationStore(root / "etc/akon/branches/develop").read()
            self.assertEqual(dev_snap.value("storage.database_path"), "/var/lib/akon/akon.sqlite")
            self.assertEqual(dev_snap.value("qbittorrent.category"), "akon")

            # Series row updated with qb.prefix = "ad" and row counts intact
            db = sqlite3.connect(root / "var/lib/akon/akon.sqlite")
            s101 = get_series(db, 101)
            self.assertEqual(s101.torrents[0]["qb"]["prefix"], "ad")
            db.close()

            # Installer state & adm launcher
            inst = json.loads((root / "var/lib/akon/installer/installer.json").read_text())
            self.assertEqual(inst["root"], "/opt/akon")
            self.assertEqual(inst["config_base"], "/etc/akon")
            self.assertEqual(inst["config_dir"], "/etc/akon/branches/develop")
            self.assertEqual(inst["state_dir"], "/var/lib/akon")
            self.assertEqual(inst["user"], "akon")
            self.assertEqual(inst["source"], "git@github.com:bugz8unny69/AniKeepOrNot.git")
            adm_text = (root / "usr/local/bin/adm").read_text()
            self.assertIn("AKON_CONFIG_DIR=/etc/akon/branches/develop", adm_text)
            self.assertFalse((root / "usr/local/bin/anime-discovery-admin").exists())

    def test_failure_in_middle_prints_undo_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_prod_layout(root)
            def fake_exec(cmd, *, check=True):
                if cmd[:2] == ["systemctl", "is-active"]:
                    return subprocess.CompletedProcess(cmd, 3, "", "")
                if cmd[0] == "groupmod":
                    raise RuntimeError("groupmod failed")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            logs = []
            with self.assertRaisesRegex(RuntimeError, "groupmod failed"):
                rename_to_akon(prefix=root, apply=True, execute=fake_exec, out=logs.append)
            self.assertTrue(any("usermod -l anime-discovery akon" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
