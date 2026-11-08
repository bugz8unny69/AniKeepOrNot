"""Offline read-only media verification and hostile filesystem cases; unrun."""
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from src.anime.acquisition.media import (parse_probe, verify_file, AudioMissing,
    MediaInvalid, MediaUnavailable, MAX_PROBE_BYTES)
from src.anime.acquisition.settings import DownloadOptions
from src.anime.acquisition.submission import SubmissionPlan, Intent
from src.anime.acquisition.metadata import parse_metainfo
from src.anime.tests.test_acquisition_metadata import metadata


def probe_bytes(*,languages=('jpn','eng'),duration='1440',container='matroska,webm'):
    streams=[{'index':0,'codec_type':'video','codec_name':'h264','width':1920,'height':1080}]
    for i,language in enumerate(languages,1):
        streams.append({'index':i,'codec_type':'audio','codec_name':'aac','tags':{'language':language}})
    return json.dumps({'format':{'format_name':container,'duration':duration},'streams':streams}).encode()


class MediaVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/('ad-'+'a'*32);self.root.mkdir()
        raw,digest=metadata()
        manifest=parse_metainfo(raw,digest)
        plan=SubmissionPlan(1,2,3,4,'a'*32,manifest)
        self.intent=Intent(plan,DownloadOptions('https://qb.example',str(self.root),'anime',30),'OWNED_STOPPED','b'*64,5)
        self.record=SimpleNamespace(fingerprint='c'*64)
        self.selected=self.root/manifest.files[0].path
        self.selected.write_bytes(b'x'*100)
        self.flag=threading.Event()

    def check(self,reader=None,**kwargs):
        return verify_file(self.intent,self.record,5,self.flag,read_probe=reader or (lambda *_:probe_bytes()),**kwargs)

    def test_checksum_and_audio_bind_actual_selected_bytes_without_writes(self):
        before=self.selected.stat();value=self.check()
        self.assertEqual(value.sha256,hashlib.sha256(b'x'*100).hexdigest())
        self.assertEqual(value.file_signature[2],100)
        self.assertEqual(value.languages,('eng','jpn'))
        self.assertEqual(value.duration_ms,1440000)
        self.assertEqual(value.download_job_id,5)
        self.assertEqual(self.selected.stat().st_mtime_ns,before.st_mtime_ns)
        self.assertEqual(list(self.root.iterdir()),[self.selected])
        self.assertNotIn(str(self.root),repr(value))

    def test_language_aliases_and_mp4_container_are_supported(self):
        self.assertEqual(parse_probe(probe_bytes(languages=('JA','EN')),'.mkv')[1],('eng','jpn'))
        self.assertEqual(parse_probe(probe_bytes(container='mov,mp4,m4a,3gp,3g2,mj2'),'.mp4')[0],1440000)

    def test_subtitles_unknown_duplicate_audio_and_commentary_do_not_satisfy_both_languages(self):
        for languages in (('jpn',),('eng','eng'),('und','eng'),('jpn','fra')):
            with self.subTest(languages=languages),self.assertRaises(AudioMissing):parse_probe(probe_bytes(languages=languages),'.mkv')
        doc=json.loads(probe_bytes());doc['streams'][2]['codec_type']='subtitle'
        with self.assertRaises(AudioMissing):parse_probe(json.dumps(doc).encode(),'.mkv')
        doc=json.loads(probe_bytes());doc['streams'][2]['disposition']={'comment':1}
        with self.assertRaises(AudioMissing):parse_probe(json.dumps(doc).encode(),'.mkv')

    def test_malformed_oversized_wrong_container_and_nonfinite_duration_fail(self):
        for raw in (b'[]',b'{broken',b'x'*(MAX_PROBE_BYTES+1),probe_bytes(duration='nan'),
                    probe_bytes(duration='0'),probe_bytes(container='mpegts')):
            with self.subTest(raw=raw[:40]),self.assertRaises(MediaInvalid):parse_probe(raw,'.mkv')
        doc=json.loads(probe_bytes());doc['streams'].append(doc['streams'][0])
        with self.assertRaises(MediaInvalid):parse_probe(json.dumps(doc).encode(),'.mkv')
        with self.assertRaises(MediaInvalid):parse_probe(b'{"format":{},"format":{},"streams":[]}', '.mkv')

    def test_truncation_symlink_hardlink_fifo_and_extra_files_preserve_everything(self):
        self.selected.write_bytes(b'x'*99)
        with self.assertRaises(MediaInvalid):self.check()
        self.selected.write_bytes(b'x'*100)
        extra=self.root/'unknown.txt';extra.write_text('keep')
        with self.assertRaises(MediaInvalid):self.check()
        self.assertEqual(extra.read_text(),'keep');extra.unlink()
        linked=self.root.parent/'linked.mkv';os.link(self.selected,linked)
        with self.assertRaises(MediaInvalid):self.check()
        linked.unlink();self.selected.unlink();self.selected.symlink_to('/etc/passwd')
        with self.assertRaises(MediaInvalid):self.check()
        self.selected.unlink();os.mkfifo(self.selected)
        with self.assertRaises(MediaInvalid):self.check()

    def test_same_size_change_and_path_replacement_during_probe_fail(self):
        def change(fd,flag):
            self.selected.write_bytes(b'y'*100)
            return probe_bytes()
        with self.assertRaises(MediaInvalid):self.check(change)
        def replace_path(fd,flag):
            self.selected.rename(self.root/'original.mkv')
            self.selected.write_bytes(b'y'*100)
            return probe_bytes()
        with self.assertRaises(MediaInvalid):self.check(replace_path)

    def test_symlinked_root_and_protected_overlap_fail(self):
        with self.assertRaises(MediaInvalid):self.check(protected_roots=(str(self.root.parent),))
        moved=self.root.with_name('moved');self.root.rename(moved);self.root.symlink_to(moved,target_is_directory=True)
        with self.assertRaises((MediaInvalid,MediaUnavailable)):self.check()

    def test_cancel_before_or_during_probe_is_not_a_success(self):
        self.flag.set()
        with self.assertRaises(asyncio.CancelledError):self.check()
        self.flag.clear()
        def cancel(fd,flag):flag.set();return probe_bytes()
        with self.assertRaises(asyncio.CancelledError):self.check(cancel)


class ProbeProcessTests(unittest.TestCase):
    def test_missing_executable_has_readable_configuration_reason(self):
        from src.anime.acquisition.media import probe, ProbeUnavailable
        from unittest.mock import patch
        with patch('src.anime.acquisition.media.subprocess.Popen',side_effect=FileNotFoundError()):
            with self.assertRaises(ProbeUnavailable):probe(3,threading.Event())

    def test_excess_output_kills_and_reaps_child(self):
        import subprocess
        import sys
        from unittest.mock import patch
        from src.anime.acquisition.media import probe
        real_popen=subprocess.Popen;children=[];commands=[]
        def start(command,**kwargs):
            commands.append((command,kwargs))
            child=real_popen([sys.executable,'-c','import sys,time;sys.stdout.buffer.write(b"x"*1100000);sys.stdout.flush();time.sleep(10)'],
                stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            children.append(child);return child
        with patch('src.anime.acquisition.media.subprocess.Popen',side_effect=start):
            with self.assertRaises(MediaInvalid):probe(3,threading.Event())
        self.assertIsNotNone(children[0].returncode)
        self.assertTrue(children[0].stdout.closed)
        self.assertNotIn('shell',commands[0][1])
        self.assertEqual(commands[0][1]['pass_fds'],(3,))
        self.assertIn('/proc/self/fd/3',commands[0][0])

    def test_deadline_kills_and_reaps_child(self):
        import subprocess
        import sys
        from unittest.mock import patch
        from src.anime.acquisition.media import probe
        real_popen=subprocess.Popen;children=[]
        def start(*args,**kwargs):
            child=real_popen([sys.executable,'-c','import time;time.sleep(10)'],
                stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            children.append(child);return child
        with patch('src.anime.acquisition.media.subprocess.Popen',side_effect=start):
            with self.assertRaises(MediaUnavailable):probe(3,threading.Event(),timeout=.02)
        self.assertIsNotNone(children[0].returncode)
        self.assertTrue(children[0].stdout.closed)
