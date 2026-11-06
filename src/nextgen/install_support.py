"""Installer-only native setup and unit rendering. No legacy configuration reader."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys

from src.nextgen.__main__ import check_local, upgrade_database, database_preview, plan_token
from src.nextgen.api.auth import public_origin
from src.nextgen.configuration import ConfigurationStore, CONFIG_NAME
from src.nextgen.configuration_schema import convert_document, decode
from src.nextgen.secrets import NAME as SECRETS_NAME, SecretStore
from src.nextgen.api.password_store import RECORD as PASSWORD_RECORD
from src.nextgen.storage.sqlite import connect, initialize


def prepare(directory,database,settings,access):
    """Called as the service user only after the installer stops both services."""
    store=ConfigurationStore(directory);settings_path=Path(directory)/CONFIG_NAME
    if settings_path.exists():
        preview=store.preview_migration()
        if preview.required:store.migrate(expected_fingerprint=preview.source_fingerprint)
        snapshot=store.read()  # Existing values always win over installer defaults.
    else:
        values=dict(settings);values['storage']={**values.get('storage',{}),'database_path':str(database)}
        snapshot=store.initialize(values).snapshot
    private=SecretStore(directory)
    if private.status().revision==0:private.initialize()
    current=private.access_credentials()
    if not current.webhook_token:
        private.update_webhook_token(access.get('webhook_token') or secrets.token_urlsafe(32),
                                     expected_revision=private.status().revision)
    path=Path(snapshot.value('storage.database_path'))
    if not path.exists():
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
        db=connect(path)
        try:initialize(db)
        finally:db.close()
    else:
        with database_preview(directory) as (_,plan):token=plan_token(plan)
        upgrade_database(directory,token)
    return check_local(directory)


class BranchSetupRefused(ValueError):
    """A branch switch cannot safely reuse the current branch's settings or data."""


# Credentials a new branch starts with; settings are converted, never copied as is.
CREDENTIAL_FILES=(SECRETS_NAME,PASSWORD_RECORD)


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def migration_lineage(source_sql_dir,version):
    """Refuse unless this branch's first `version` migrations are the source branch's, byte for byte.

    A version number alone is not enough: two branches can both reach "v65"
    with different schemas, or the same tables but different data rewrites.
    Identical migration files up to the database's version mean this branch
    would have built exactly the same database.
    """
    from src.nextgen.storage.migrations import LATEST_VERSION, SCRIPTS, SQL_DIR
    if type(version) is not int or version<1:
        raise BranchSetupRefused('The current database has no recognised schema version.')
    if version>LATEST_VERSION:
        raise BranchSetupRefused(f'The current database is at schema v{version}, newer than this branch '
                                 f'understands (v{LATEST_VERSION}).')
    theirs=sorted(Path(source_sql_dir).glob('[0-9][0-9][0-9]_*.sql'))
    if len(theirs)<version:
        raise BranchSetupRefused('The current branch\'s migration files are missing, so its database cannot be checked.')
    for number,(ours,their) in enumerate(zip(SCRIPTS[:version],theirs[:version]),start=1):
        if ours!=their.name or _sha256(SQL_DIR/ours)!=_sha256(their):
            raise BranchSetupRefused(f'The two branches\' database histories differ at migration {number} '
                                     f'({their.name} on the current branch, {ours} on this one). '
                                     'The data cannot be copied; this branch starts with a new database.')
    return version


def copy_database(source,target,source_sql_dir):
    """A consistent copy of the source database for a new branch (services are stopped)."""
    source=Path(source);target=Path(target)
    if not source.is_file():raise BranchSetupRefused('The current branch has no database to copy.')
    src=sqlite3.connect(source.absolute().as_uri()+'?mode=ro',uri=True)
    try:
        version=migration_lineage(source_sql_dir,src.execute('PRAGMA user_version').fetchone()[0])
        if src.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
            raise BranchSetupRefused('The current database failed its integrity check.')
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
        try:
            dst=sqlite3.connect(target)
            try:src.backup(dst)
            finally:dst.close()
        except BaseException:
            target.unlink(missing_ok=True);raise
    finally:
        src.close()
    return version


def branch_setup(directory,source_directory,database,source_sql_dir,copy_data):
    """First use of a branch: its own settings, credentials and database.

    Called as the service user after the installer stops both services. The
    settings are converted by this (the target) branch's own upgrade, so a
    branch never runs on settings it does not understand.
    """
    target=Path(directory);source=Path(source_directory)
    if (target/CONFIG_NAME).exists():raise BranchSetupRefused('This branch already has settings.')
    try:previous=convert_document(decode((source/CONFIG_NAME).read_bytes()))
    except (ValueError,OSError):
        raise BranchSetupRefused('This branch cannot read the current settings (they may be newer than it '
                                 'understands). Set the branch up fresh instead.') from None
    for name in CREDENTIAL_FILES:
        if (source/name).is_file():
            fd=os.open(target/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write((source/name).read_bytes())
    copied=None
    if copy_data:
        copied=copy_database(previous.value('storage.database_path'),database,source_sql_dir)
    values=previous.to_document()['settings']
    values['storage']={**values['storage'],'database_path':str(database)}
    ConfigurationStore(target).initialize(values)
    return {'database':str(database),'copied_from_version':copied}


def quoted(value):
    if type(value) is not str or any(ord(c)<32 or ord(c)==127 for c in value):raise ValueError('Invalid unit value.')
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'


def units(snapshot,root,config_dir,state_dir,user,origin):
    if not re.fullmatch('[a-z_][a-z0-9_-]{0,31}',user):raise ValueError('Invalid service account.')
    host=snapshot.value('api.host')
    authority='['+host+']' if ':' in host else host
    origin=public_origin(origin or 'http://'+authority+':'+str(snapshot.value('api.port')))
    if host!='localhost' and not ipaddress.ip_address(host).is_loopback:raise ValueError('The API listener must be loopback.')
    current=str(Path(root)/'current');python=current+'/.venv/bin/python'
    # Installer infrastructure paths do not contain dollars; systemd expands
    # dollars in ExecStart even inside quoted arguments.
    if any('$' in str(p) for p in (root,config_dir,state_dir)):raise ValueError('Use infrastructure paths without dollar signs.')
    api=' '.join(map(quoted,(python,'-m','src.nextgen.api','serve','--config-dir',str(config_dir),'--public-origin',origin)))
    worker=' '.join(map(quoted,(python,'-m','src.nextgen','worker','--config-dir',str(config_dir))))
    roots=[snapshot.value('storage.'+name) for name in ('download_root','trial_root','keep_root') if snapshot.value('storage.'+name)]
    db_parent=str(Path(snapshot.value('storage.database_path')).parent)
    base=[str(state_dir),str(config_dir),db_parent]
    def service(label,command,media=False):
        mounts=('RequiresMountsFor='+' '.join(quoted(p) for p in roots)+'\n') if media and roots else ''
        dependencies='Requires=anime-discovery-api.service\nBindsTo=anime-discovery-api.service\nAfter=anime-discovery-api.service\n' if media else ''
        runtime='' if media else 'Environment=ANIME_DISCOVERY_ADMIN_SOCKET=/run/anime-discovery/admin.sock\nRuntimeDirectory=anime-discovery\nRuntimeDirectoryMode=0700\n'
        return ('[Unit]\nStartLimitIntervalSec=300\nStartLimitBurst=5\nDescription=Anime Discovery '+label+'\nPartOf=anime-discovery.target\nWants=network-online.target\nAfter=network-online.target remote-fs.target\n'+dependencies+mounts+
            '\n[Service]\nType=notify\nNotifyAccess=main\nWatchdogSec=60s\n'+runtime+'User='+user+'\nGroup='+user+'\nWorkingDirectory='+current.replace('%','%%')+
            '\nEnvironment=PYTHONUNBUFFERED=1\nExecStart='+command+'\nRestart=on-failure\nRestartSec=10\nKillMode=mixed\nTimeoutStopSec=infinity\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\nProtectHome=true\nProtectSystem=strict\nReadWritePaths='+
            ' '.join(quoted(p) for p in dict.fromkeys(base+(roots if media else [])))+'\nStandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=multi-user.target\n')
    return {'anime-discovery-api.service':service('API',api),
        'anime-discovery-worker.service':service('worker',worker,True),
        'anime-discovery.target':'[Unit]\nDescription=Anime Discovery\nWants=anime-discovery-api.service anime-discovery-worker.service\nAfter=network-online.target remote-fs.target\n\n[Install]\nWantedBy=multi-user.target\n'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('prepare','branch-setup','render','check-worker'))
    parser.add_argument('--config-dir',required=True)
    args=parser.parse_args(argv)
    try:
        document=decode(sys.stdin.buffer.read(65537))
        if type(document) is not dict:raise ValueError()
        if args.command=='prepare':result=prepare(args.config_dir,document['database'],document.get('settings',{}),document.get('access',{}))
        elif args.command=='branch-setup':
            try:result=branch_setup(args.config_dir,document['source_config_dir'],document['database'],
                                    document['source_sql_dir'],document['copy_data'] is True)
            except BranchSetupRefused as exc:
                print('BRANCH_SETUP_REFUSED: '+str(exc),file=sys.stderr);return 2
        elif args.command=='render':result=units(ConfigurationStore(args.config_dir).read(),document['root'],args.config_dir,document['state_dir'],document['user'],document['origin'])
        else:result=check_local(args.config_dir,worker=True)
        print(json.dumps(result,sort_keys=True));return 0
    except Exception:
        print('INSTALL_SETUP_FAILED: Native configuration, database or local readiness needs review; services remain stopped.',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
