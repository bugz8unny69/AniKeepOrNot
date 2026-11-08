"""Offline bounded qB transport and stopped add cases; intentionally unexecuted."""
from email.message import Message
import io
import json
import unittest

from src.nextgen.torrent_client.qbittorrent import Client
from src.nextgen.acquisition.settings import DownloadOptions, DownloadCredentials
from src.nextgen.acquisition.submission import (ClientInvalid, ClientUnavailable, ClientAuthenticationFailed,
                                               ClientUnsupported, Intent, SubmissionPlan, SendPermit)
from src.nextgen.acquisition.metadata import parse_metainfo
from src.nextgen.tests.test_acquisition_metadata import metadata


class Response(io.BytesIO):
    def __init__(self,body,status=200,*,cookie=False):
        super().__init__(body)
        self.status=status
        self.headers=Message()
        if cookie:self.headers['Set-Cookie']='SID=private_session_12345678; HttpOnly; path=/'
    def getcode(self):return self.status


class QBTransportTests(unittest.TestCase):
    def client(self,responses):
        self.requests=[]
        def transport(request,timeout):
            self.requests.append((request,timeout))
            return responses.pop(0)
        return Client(DownloadOptions('https://qb.example','/srv/downloads/ad-marker','anime-discovery',10),
                      DownloadCredentials('private-user','private-pass'),transport=transport)

    def login_responses(self,version=b'v5.0.0'):
        return [Response(b'Ok.',cookie=True),Response(version),Response(b'2.8.19' if version.startswith(b'v4.') else b'2.11.0')]

    def test_base64_session_cookie_is_preserved_on_authenticated_requests(self):
        for version in (b'v4.5.2',b'v5.0.0'):
            for sid in ('abcd+efg/hijklmnopqrstuvwxy0123456', 'abcd+efg/hijklmnopqrstuvwxy01234=='):
                with self.subTest(version=version,sid=sid):
                    responses=self.login_responses(version)+[Response(b'[]')]
                    responses[0].headers.replace_header('Set-Cookie',
                        'SID='+sid+'; HttpOnly; path=/')
                    client=self.client(responses)
                    client.login()
                    self.assertIsNone(client.inspect('a'*40))
                    self.assertIsNone(self.requests[0][0].get_header('Cookie'))
                    self.assertEqual(len(self.requests),4)
                    for request,_ in self.requests[1:]:
                        self.assertEqual(request.get_header('Cookie'),'SID='+sid)

    def test_invalid_or_duplicate_session_cookies_are_rejected_before_api_calls(self):
        for cookies in ([], ['SID=short'], ['SID='+'a'*257],
                        ['SID="abcdefgh;injected=value"'], ['SID="abcdefgh value"'],
                        ['SID="abcdefgh\\015\\012value"'],
                        ['SID=abcdefgh', 'SID=ijklmnop']):
            with self.subTest(cookies=cookies):
                response=Response(b'Ok.')
                for cookie in cookies:response.headers['Set-Cookie']=cookie
                client=self.client([response])
                with self.assertRaises((ClientAuthenticationFailed,ClientInvalid)):client.login()
                self.assertEqual(len(self.requests),1)

    def test_login_and_version_specific_stopped_upload_have_no_start_or_retry(self):
        raw,digest=metadata()
        for version,parameter in ((b'v5.0.0',b'name="stopped"'),(b'v4.5.2',b'name="paused"')):
            client=self.client(self.login_responses(version)+[Response(b'Ok.')])
            client.login()
            plan=SubmissionPlan(1,1,2,3,'a'*32,parse_metainfo(raw,digest))
            intent=Intent(plan,client.options,'UNCERTAIN','b'*64,client.major)
            permit=SendPermit(intent.fingerprint)
            client.add_stopped(raw,intent,permit)
            request,timeout=self.requests[-1]
            self.assertEqual(request.full_url,'https://qb.example/api/v2/torrents/add')
            self.assertIn(parameter+b'\r\n\r\ntrue',request.data)
            self.assertIn(b'name="autoTMM"\r\n\r\nfalse',request.data)
            self.assertIn(b'name="useDownloadPath"\r\n\r\nfalse',request.data)
            self.assertIn(b'name="contentLayout"\r\n\r\nOriginal',request.data)
            self.assertEqual(timeout,10)
            self.assertEqual(len(self.requests),4)
            with self.assertRaises(ClientInvalid):client.add_stopped(raw,intent,permit)
            self.assertEqual(len(self.requests),4)

    def test_redirect_authentication_unsupported_versions_and_response_bounds(self):
        for responses,error in (([Response(b'',302)],ClientUnavailable),([Response(b'Fails.')],ClientAuthenticationFailed),
                                (self.login_responses(b'v3.0.0'),ClientUnsupported),
                                ([Response(b'x'*4097)],ClientInvalid)):
            client=self.client(responses)
            with self.assertRaises(error):client.login()
        client=self.client([Response(b'Ok.')])
        with self.assertRaises(ClientAuthenticationFailed):client.login()

    def test_filtered_inventory_is_authoritative_only_when_well_formed(self):
        raw,digest=metadata()
        row={'hash':digest,'state':'stoppedDL','save_path':'/srv/downloads/ad-marker',
             'category':'anime-discovery','tags':'ad-'+('a'*32),'auto_tmm':False}
        files=[{'index':0,'name':'Test Anime - 10.mkv','size':100}]
        client=self.client(self.login_responses()+[Response(json.dumps([row]).encode()),Response(json.dumps(files).encode())])
        client.login()
        found=client.inspect(digest)
        self.assertEqual(found.files,parse_metainfo(raw,digest).files)
        self.assertIn('hashes='+digest,self.requests[-2][0].full_url)
        for response in (b'{}',b'[{},{}]',b'<html>login</html>',b'[NaN]'):
            client=self.client(self.login_responses()+[Response(response)])
            client.login()
            with self.assertRaises(ClientInvalid):client.inspect(digest)

    def test_absence_has_no_mutation_and_bad_file_paths_are_rejected(self):
        _,digest=metadata()
        client=self.client(self.login_responses()+[Response(b'[]')])
        client.login()
        self.assertIsNone(client.inspect(digest))
        self.assertTrue(all('/torrents/add' not in r.full_url for r,_ in self.requests))
        row={'hash':digest,'state':'stoppedDL','save_path':'/srv/downloads/ad-marker',
             'category':'anime-discovery','tags':'','auto_tmm':False}
        client=self.client(self.login_responses()+[Response(json.dumps([row]).encode()),
                           Response(b'[{"index":0,"name":"../escape.mkv","size":100}]')])
        client.login()
        with self.assertRaises(ClientInvalid):client.inspect(digest)

    def test_ownership_confirmation_checks_all_saved_fields_and_stopped_inventory(self):
        from dataclasses import replace
        from src.nextgen.acquisition.submission import Observation, classify
        from src.nextgen.acquisition.metadata import TorrentFile
        raw,digest=metadata()
        plan=SubmissionPlan(1,1,2,3,'a'*32,parse_metainfo(raw,digest))
        options=DownloadOptions('https://qb.example','/srv/downloads/ad-marker','anime-discovery',10)
        intent=Intent(plan,options,'UNCERTAIN','b'*64,5)
        found=Observation(digest,'stoppedDL',options.download_root,options.category,('ad-'+plan.marker,),False,plan.manifest.files)
        self.assertEqual(classify(intent,found),('OWNED_STOPPED','TORRENT_STOPPED_CONFIRMED'))
        changes=({'info_hash':'c'*40},{'save_path':'/other'},{'category':'other'},{'tags':()},
                 {'auto_tmm':True},{'state':'downloading'},
                 {'files':(TorrentFile(0,'Test Anime - 10.mkv',101),)})
        for change in changes:self.assertEqual(classify(intent,replace(found,**change))[0],'REVIEW')
        self.assertEqual(classify(intent,None)[0],'UNCERTAIN')
        self.assertEqual(classify(intent,replace(found,files=()))[0],'UNCERTAIN')
