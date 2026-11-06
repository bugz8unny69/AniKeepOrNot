"""Installer-only native setup and unit rendering. No legacy configuration reader."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sys

from src.nextgen.__main__ import check_local, upgrade_database, database_preview, plan_token
from src.nextgen.api.auth import public_origin
from src.nextgen.configuration import ConfigurationStore, CONFIG_NAME
from src.nextgen.configuration_schema import decode
from src.nextgen.secrets import SecretStore
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
        # Worker exit 78 (services/startup.py): a required service is not set
        # up or rejected its login; restarting cannot fix it, so do not loop.
        no_restart='RestartPreventExitStatus=78\n' if media else ''
        runtime='' if media else 'Environment=ANIME_DISCOVERY_ADMIN_SOCKET=/run/anime-discovery/admin.sock\nRuntimeDirectory=anime-discovery\nRuntimeDirectoryMode=0700\n'
        return ('[Unit]\nStartLimitIntervalSec=300\nStartLimitBurst=5\nDescription=Anime Discovery '+label+'\nPartOf=anime-discovery.target\nWants=network-online.target\nAfter=network-online.target remote-fs.target\n'+dependencies+mounts+
            '\n[Service]\nType=notify\nNotifyAccess=main\nWatchdogSec=60s\n'+runtime+'User='+user+'\nGroup='+user+'\nWorkingDirectory='+current.replace('%','%%')+
            '\nEnvironment=PYTHONUNBUFFERED=1\nExecStart='+command+'\nRestart=on-failure\nRestartSec=10\n'+no_restart+'KillMode=mixed\nTimeoutStopSec=infinity\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\nProtectHome=true\nProtectSystem=strict\nReadWritePaths='+
            ' '.join(quoted(p) for p in dict.fromkeys(base+(roots if media else [])))+'\nStandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=multi-user.target\n')
    return {'anime-discovery-api.service':service('API',api),
        'anime-discovery-worker.service':service('worker',worker,True),
        'anime-discovery.target':'[Unit]\nDescription=Anime Discovery\nWants=anime-discovery-api.service anime-discovery-worker.service\nAfter=network-online.target remote-fs.target\n\n[Install]\nWantedBy=multi-user.target\n'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('prepare','render','check-worker'))
    parser.add_argument('--config-dir',required=True)
    args=parser.parse_args(argv)
    try:
        document=decode(sys.stdin.buffer.read(65537))
        if type(document) is not dict:raise ValueError()
        if args.command=='prepare':result=prepare(args.config_dir,document['database'],document.get('settings',{}),document.get('access',{}))
        elif args.command=='render':result=units(ConfigurationStore(args.config_dir).read(),document['root'],args.config_dir,document['state_dir'],document['user'],document['origin'])
        else:result=check_local(args.config_dir,worker=True)
        print(json.dumps(result,sort_keys=True));return 0
    except Exception:
        print('INSTALL_SETUP_FAILED: Native configuration, database or local readiness needs review; services remain stopped.',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
