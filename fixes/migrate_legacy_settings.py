#!/usr/bin/env python3
"""One-time, explicit legacy-settings import. Never reads an operational database."""
import argparse
import fcntl
import uuid
import hashlib
import json
import os
import pwd
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.nextgen.configuration_schema import ConfigurationSnapshot, decode, encode
from src.nextgen.secrets import _value, _download_values, _access_values

MAPPINGS={
    'daemon.concurrency':'worker.concurrency','web.port':'api.port',
    'storage.download_root':'storage.download_root','storage.trial_root':'storage.user_evaluation_root',
    'anidb.client':'anidb.client_name','anidb.clientver':'anidb.client_version',
    'trial.max_episode_mb':'acquisition.max_episode_mb','trial.max_six_episode_mb':'acquisition.max_plan_mb',
    'qbittorrent.base_url':'qbittorrent.base_url','sonarr.quality_profile_id':'sonarr.quality_profile_id',
    'sonarr.root_folder':'sonarr.library_root',
}
SECRET_KEYS=('tvdb_api_key','sonarr_api_key','jellyfin_api_key','shoko_api_key')
MARKER='.legacy-settings-import.json'


def read_object(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('Expected a regular settings file.')
        raw=stream.read(65537)
    data=decode(raw)
    if type(data) is not dict:raise ValueError('Expected a JSON object.')
    return raw,data


def put(settings,path,value):
    section,key=path.split('.')
    settings.setdefault(section,{})[key]=value


def safe_name(name):return name if re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',name) else '<unrecognized key>'


def plan(source,database,overrides=None):
    raw,settings=read_object(Path(source)/'settings.json')
    secret_raw,secrets=read_object(Path(source)/'secrets.json')
    if 'schema_version' in settings:raise ValueError('Source is already versioned; use the Anime Discovery configuration upgrade command.')
    if any(type(section) is not dict for section in settings.values()):raise ValueError('Legacy settings sections must be objects.')
    overrides={} if overrides is None else overrides
    if type(overrides) is not dict:raise ValueError('Overrides must be a Anime Discovery settings object.')
    mapped={};used=set();warnings=[]
    for old,new in MAPPINGS.items():
        section,key=old.split('.')
        if key in settings.get(section,{}):put(mapped,new,settings[section][key]);used.add(old)
    if 'root_folder' in settings.get('sonarr',{}):
        put(mapped,'storage.keep_root',settings['sonarr']['root_folder'])
        warnings.append('Confirm that the imported permanent root is the same storage visible locally and in Sonarr.')
    host=settings.get('web',{}).get('host','127.0.0.1')
    if host not in ('127.0.0.1','::1','localhost'):host='127.0.0.1';warnings.append('Public legacy web binding becomes loopback; configure the public HTTPS origin in the installer.')
    put(mapped,'api.host',host);used.add('web.host')
    for name in ('sonarr','jellyfin','shoko'):
        values=settings.get(name,{})
        if 'enabled' in values and type(values['enabled']) is not bool:raise ValueError('Legacy service enable flags must be boolean.')
        put(mapped,name+'.base_url',values.get('base_url','') if values.get('enabled',False) else '')
        used.update((name+'.base_url',name+'.enabled'))
    # Legacy operational SQLite is intentionally not a Anime Discovery input.
    put(mapped,'storage.database_path',str(database))
    for section,values in overrides.items():
        if type(values) is not dict:raise ValueError('Override sections must be objects.')
        mapped.setdefault(section,{}).update(values)
    if mapped['storage']['database_path']!=str(database):raise ValueError('Set the new database through --database, not an override.')
    snapshot=ConfigurationSnapshot(1,mapped)
    values={key:_value(secrets.get(key,'')) for key in SECRET_KEYS}
    values.update(_download_values(settings.get('qbittorrent',{}).get('username',''),secrets.get('qbittorrent_password','')))
    webhook=secrets.get('web_token','')
    if type(webhook) is not str:raise ValueError('Legacy webhook credential must be a string.')
    if webhook and not re.fullmatch(r'[A-Za-z0-9_-]{32,256}',webhook):
        webhook='';warnings.append('The old webhook token is incompatible; setup must generate or accept a new token.')
    values.update(_access_values(webhook,''))
    values.pop('management_token',None)
    used.add('qbittorrent.username')
    skipped=sorted(safe_name(section+'.'+key) for section,fields in settings.items() for key in fields if section+'.'+key not in used)
    skipped_secrets=sorted(safe_name(key) for key in secrets if key not in (*SECRET_KEYS,'qbittorrent_password','web_token'))
    fingerprint=hashlib.sha256(raw+b'\0'+secret_raw+b'\0'+json.dumps(overrides,sort_keys=True).encode()+b'\0'+str(database).encode()).hexdigest()
    report={'source_fingerprint':fingerprint,'mapped_settings':sorted(path for section,fields in mapped.items() for path in [section+'.'+k for k in fields]),
        'retained_legacy_settings':skipped,'retained_legacy_credentials':skipped_secrets,
        'credential_presence':{k:bool(v) for k,v in values.items()},'warnings':warnings,
        'legacy_database_imported':False,'source_modified':False}
    return snapshot,values,report


def write_private(path,raw,uid,gid):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        if (uid,gid)!=(os.geteuid(),os.getegid()):os.fchown(stream.fileno(),uid,gid)
        stream.write(raw);stream.flush();os.fsync(stream.fileno())


def migrate(source,output,database,*,apply=False,overrides=None,expected_fingerprint=None,uid=None,gid=None):
    output=Path(output)
    if output.resolve()==Path(source).resolve():raise ValueError('Import into a separate Anime Discovery directory.')
    snapshot,values,report=plan(source,database,overrides)
    if expected_fingerprint and expected_fingerprint!=report['source_fingerprint']:raise ValueError('Source settings changed; preview again.')
    if output.exists() or output.is_symlink():
        if output.is_symlink():raise ValueError('Destination must not be a symlink.')
        try:_,marker=read_object(output/MARKER)
        except FileNotFoundError:raise ValueError('Existing Anime Discovery settings are preserved; choose a new output directory.') from None
        if marker.get('source_fingerprint')!=report['source_fingerprint']:raise ValueError('This destination was imported from different inputs.')
        # Validate the installed files without overwriting subsequent user edits.
        read_object(output/'settings.json');read_object(output/'secrets.json')
        return {**report,'already_imported':True,'changed':False}
    if not apply:return {**report,'already_imported':False,'changed':False}
    uid=os.geteuid() if uid is None else uid;gid=os.getegid() if gid is None else gid
    folder=Path(tempfile.mkdtemp(prefix='.nextgen-import-',dir=output.parent))
    try:
        if (uid,gid)!=(os.geteuid(),os.getegid()):os.chown(folder,uid,gid)
        write_private(folder/'settings.json',encode(snapshot),uid,gid)
        raw=(json.dumps({'schema_version':7,'revision':1,'credentials':values},sort_keys=True)+'\n').encode()
        write_private(folder/'secrets.json',raw,uid,gid)
        write_private(folder/MARKER,(json.dumps(report,sort_keys=True)+'\n').encode(),uid,gid)
        # rename never replaces a populated destination. A concurrent empty
        # destination is not accepted either; serialize through installer lock.
        if output.exists() or output.is_symlink():raise ValueError('Destination appeared during import.')
        os.rename(folder,output)
        fd=os.open(output.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if folder.exists():shutil.rmtree(folder)
    return {**report,'already_imported':False,'changed':True}


def fix_in_place(directory, *, uid=None, gid=None):
    directory=Path(directory)
    if directory.is_symlink():raise ValueError('Configuration directory must not be a symlink.')
    uid=directory.stat().st_uid if uid is None else uid
    gid=directory.stat().st_gid if gid is None else gid
    lock=os.open(directory/'.settings-conversion.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        pending=directory/'.settings-conversion-pending.json'
        if pending.exists():
            _,journal=read_object(pending)
            backup=directory/journal['backup']
            if backup.parent!=directory or not backup.name.startswith('.settings-backup-'):
                raise ValueError('Invalid recovery backup.')
            for name in ('settings.json','secrets.json'):
                raw,_=read_object(backup/name)
                replace_private(directory/name,raw,uid,gid)
            pending.unlink()
        _,settings=read_object(directory/'settings.json')
        if 'schema_version' in settings:
            from src.nextgen.configuration import ConfigurationStore
            from src.nextgen.secrets import SecretStore
            ConfigurationStore(directory).read()
            SecretStore(directory).status()
            return {'changed':False,'message':'Settings are already converted.'}
        snapshot,values,report=plan(directory,Path('/var/lib/anime-discovery/anime-discovery.sqlite'))
        backup=directory/('.settings-backup-'+uuid.uuid4().hex)
        backup.mkdir(mode=0o700)
        for name in ('settings.json','secrets.json'):
            raw,_=read_object(directory/name)
            write_private(backup/name,raw,os.geteuid(),os.getegid())
        write_private(pending,json.dumps({'backup':backup.name}).encode(),os.geteuid(),os.getegid())
        sync_directory(directory)
        try:
            replace_private(directory/'settings.json',encode(snapshot),uid,gid)
            replace_private(directory/'secrets.json',json.dumps({'schema_version':7,'revision':1,'credentials':values}).encode(),uid,gid)
            sync_directory(directory)
            pending.unlink()
            sync_directory(directory)
        except BaseException:
            # Leave the journal for deterministic recovery on the next run.
            raise
        return {'changed':True,'backup':str(backup),'warnings':report['warnings'],
                'retained_legacy_settings':report['retained_legacy_settings'],
                'retained_legacy_credentials':report['retained_legacy_credentials']}
    finally:os.close(lock)


def sync_directory(directory):
    fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def replace_private(path,raw,uid,gid):
    temporary=path.with_name('.convert-'+uuid.uuid4().hex)
    try:
        write_private(temporary,raw,uid,gid)
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description='One-time in-place settings compatibility fix. Stop the old service before running.')
    parser.add_argument('--config-dir',type=Path,default=Path('/etc/anime-discovery'))
    args=parser.parse_args(argv)
    try:
        result=fix_in_place(args.config_dir)
        print(json.dumps(result,sort_keys=True))
        return 0
    except (OSError,ValueError,TypeError,RuntimeError):
        print('SETTINGS_FIX_FAILED: Conversion could not complete. Original files are retained in any .settings-backup-* directory; rerun to recover an interrupted conversion. Stop the old service before retrying.',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
