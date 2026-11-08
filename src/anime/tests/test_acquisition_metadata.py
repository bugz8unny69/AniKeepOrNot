"""Offline BEP3 exact-byte inventory cases. Intentionally unexecuted."""
import hashlib
import unittest
from src.anime.acquisition.metadata import (
    MetadataInvalid, SourceMissing, parse_metainfo, parse_batch_metainfo,
    BatchInventory, MAX_METAINFO,
)


def bencode(value):
    if type(value) is bytes:return str(len(value)).encode()+b':'+value
    if type(value) is int:return b'i'+str(value).encode()+b'e'
    if type(value) is list:return b'l'+b''.join(bencode(v) for v in value)+b'e'
    if type(value) is dict:return b'd'+b''.join(bencode(k)+bencode(v) for k,v in sorted(value.items()))+b'e'
    raise TypeError()


def metadata(*,name=b'Test Anime - 10.mkv',files=None,extra=None):
    info={b'name':name,b'piece length':262144,b'pieces':b'p'*20}
    if files is None:info[b'length']=100
    else:info[b'files']=files
    if extra:info.update(extra)
    encoded=bencode(info)
    return bencode({b'announce':b'https://tracker.invalid/private-secret',b'info':info}),hashlib.sha1(encoded).hexdigest()


class TorrentMetadataTests(unittest.TestCase):
    def test_single_video_hashes_exact_info_bytes_and_ignores_tracker_secrets(self):
        raw,digest=metadata()
        result=parse_metainfo(raw,digest.upper())
        self.assertEqual(result.info_hash,digest)
        self.assertEqual([(f.index,f.path,f.size) for f in result.files],[(0,'Test Anime - 10.mkv',100)])
        self.assertNotIn('private-secret',result.json()+repr(result))
        self.assertEqual(result.selected_index,0)
        with self.assertRaises(MetadataInvalid):parse_metainfo(raw,'0'*40)

    def test_multifile_sidecars_preserve_metadata_order(self):
        raw,digest=metadata(name=b'Folder',files=[{b'length':10,b'path':[b'info.nfo']},
                                                    {b'length':100,b'path':[b'Test Anime - 10.mkv']}])
        result=parse_metainfo(raw,digest)
        self.assertEqual(result.selected_index,1)
        self.assertEqual([f.path for f in result.files],['Folder/info.nfo','Folder/Test Anime - 10.mkv'])

    def test_batch_inventory_can_hold_shiki_ova_without_authorizing_selection(self):
        files=[{b'length':100,b'path':[b'Season 01', f'Shiki S01E{i:02d}.mkv'.encode()]}
               for i in range(1,13)]
        files += [{b'length':50,b'path':[b'OVA',b'Shiki OVA 01.mkv']},
                  {b'length':1,b'path':[b'info.nfo']}]
        raw,digest=metadata(name=b'Shiki',files=files)
        batch=parse_batch_metainfo(raw,digest)
        self.assertIs(type(batch),BatchInventory)
        self.assertEqual(len(batch.files),14)
        self.assertNotIn('selected_index',batch.json())
        with self.assertRaises(MetadataInvalid):parse_metainfo(raw,digest)
        with self.assertRaises(MetadataInvalid):parse_batch_metainfo(raw,'0'*40)

    def test_batch_inventory_still_rejects_unsafe_payloads(self):
        cases=(
            [{b'length':100,b'path':[b'Show E01.mkv']},
             {b'length':1,b'path':[b'run.exe']}],
            [{b'length':100,b'path':[b'Show E01.mkv']},
             {b'length':1,b'path':[b'../escape.mkv']}],
            [{b'length':100,b'path':[b'Show E01.mkv']},
             {b'length':1,b'path':[b'show e01.MKV']}],
            [{b'length':100,b'path':[b'Show E01.mkv'],b'attr':b'l'}],
            [{b'length':1,b'path':[b'only.nfo']}],
            [{b'length':0,b'path':[b'empty.mkv']}],
        )
        for files in cases:
            with self.subTest(files=files):
                raw,digest=metadata(name=b'Show',files=files)
                with self.assertRaises(MetadataInvalid):parse_batch_metainfo(raw,digest)

    def test_packs_unsafe_aliases_links_and_extra_payloads_are_reviewed(self):
        cases=[
            dict(files=[{b'length':100,b'path':[b'Test Anime - 10.mkv']},{b'length':100,b'path':[b'Test Anime - 11.mkv']}]),
            dict(files=[{b'length':100,b'path':[b'..',b'Test Anime - 10.mkv']}]),
            dict(files=[{b'length':100,b'path':[b'/Test Anime - 10.mkv']}]),
            dict(files=[{b'length':100,b'path':[b'Test Anime - 10.mkv'],b'attr':b'l'}]),
            dict(files=[{b'length':100,b'path':[b'Test Anime - 10.mkv']},{b'length':1,b'path':[b'run.exe']}]),
            dict(name=b'../Test Anime - 10.mkv'),dict(name=b'Test Anime - 10.mkv\x00'),
            dict(extra={b'name.utf-8':b'Different - 1.mkv'}),dict(extra={b'meta version':2}),
            dict(extra={b'pieces':b'p'*19}),dict(extra={b'length':-1}),
            dict(files=[]),dict(extra={b'piece length':0}),
            dict(files=[{b'length':100,b'path':[b'Test Anime - 10.mkv']},
                        {b'length':1,b'path':[b'test anime - 10.MKV']}]),
        ]
        for case in cases:
            with self.subTest(case=case):
                raw,digest=metadata(**case)
                with self.assertRaises(MetadataInvalid):parse_metainfo(raw,digest)

    def test_noncanonical_encoding_truncation_trailing_data_and_bounds(self):
        raw,digest=metadata()
        for bad in (raw[:-1],raw+b'junk',raw.replace(b'i100e',b'i0100e'),
                    b'd4:infoi0e4:infoi0ee',b'l'+b'l'*17+b'e'*18,b'x'*(MAX_METAINFO+1)):
            with self.assertRaises(MetadataInvalid):parse_metainfo(bad,digest)
        with self.assertRaises(SourceMissing):parse_metainfo(raw,'a'*64)
