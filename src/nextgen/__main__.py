"""Explicit local setup/maintenance and worker entry point; no work on import."""
import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys

from src.nextgen.configuration import ConfigurationStore
from src.nextgen.configuration_schema import ConfigurationSnapshot, decode
from src.nextgen.maintenance import MaintenanceGate, MaintenancePending
from src.nextgen.secrets import SecretStore
from src.nextgen.services.startup import EXIT_CODE as STARTUP_EXIT_CODE, StartupRefused
from src.nextgen.storage import migrations
from src.nextgen.storage.sqlite import connect, initialize


def plan_token(plan):
    return hashlib.sha256(json.dumps(asdict(plan),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def initialize_local(directory,database,settings=None):
    """Explicit fresh install only; refuse existing files and directories as DB."""
    database=Path(database)
    if not database.is_absolute():raise ValueError('Use an absolute local database path.')
    values={} if settings is None else dict(settings)
    values['storage']={**values.get('storage',{}),'database_path':str(database)}
    snapshot=ConfigurationSnapshot(1,values)  # Validate before writing anything.
    from src.nextgen.configuration import CONFIG_NAME
    if (Path(directory)/CONFIG_NAME).exists() or (Path(directory)/'secrets.json').exists():raise ValueError('Configuration or credentials already exist. Use the explicit upgrade commands.')
    # The account and parent directories are provisioned by the operator.
    fd=os.open(database,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    os.close(fd)
    db=connect(database)
    try:initialize(db)
    finally:db.close()
    result=ConfigurationStore(directory).initialize(snapshot.to_document()['settings'])
    credentials=SecretStore(directory).initialize()
    return {'initialized':True,'database_schema_version':migrations.LATEST_VERSION,
        'configuration_version':snapshot.schema_version,
        'durability_confirmed':result.durability_confirmed and credentials.durability_confirmed}


@contextmanager
def database_preview(directory):
    snapshot=ConfigurationStore(directory).read()
    database=snapshot.value('storage.database_path');gate=MaintenanceGate(database)
    ticket=gate.status()
    # Recovery previews preserve a previous failed-upgrade maintenance marker.
    with (gate.exclusive(ticket) if ticket is not None else gate.operation()):
        yield database,migrations.preview(database)


def upgrade_database(directory,expected_token):
    with database_preview(directory) as (database,plan):
        if plan_token(plan)!=expected_token:raise ValueError('Database plan changed. Preview it again.')
    gate=MaintenanceGate(database);ticket=gate.request()
    with gate.exclusive(ticket) as lease:
        current=migrations.preview(database)
        if current!=plan:raise ValueError('Database plan changed; maintenance remains active.')
        result=migrations.apply(database,current,lease=lease)
        # Services must be stopped and the installed source must match this CLI.
        if migrations.preview(database).from_version!=migrations.LATEST_VERSION:raise ValueError('Database is not current; maintenance remains active.')
        ConfigurationStore(directory).read()
        lease.complete()
    # The upgrade succeeded: keep the two newest pre-upgrade copies only.
    return {**asdict(result),'removed_backups':migrations.prune_backups(database)}


def check_local(directory,*,worker=False):
    snapshot=ConfigurationStore(directory).read()
    if MaintenanceGate(snapshot.value('storage.database_path')).status() is not None:raise MaintenancePending()
    with database_preview(directory) as (_,plan):
        if plan.from_version!=migrations.LATEST_VERSION:raise ValueError('Run the explicit database upgrade before starting services.')
    credentials=SecretStore(directory).status()
    # API must bootstrap before webhook credentials are configured through Settings.
    # Worker validation enforces its provider requirements independently.
    if worker:
        if os.geteuid()==0:raise ValueError('Run the worker as the dedicated non-root service account.')
        from src.nextgen.providers.configuration import validate_provider_configuration
        validate_provider_configuration(snapshot,secrets=SecretStore(directory))
        if not os.access('/usr/bin/ffprobe',os.X_OK):raise ValueError('Install /usr/bin/ffprobe before starting the worker.')
    return {'local_ready':True,'database_schema_version':migrations.LATEST_VERSION,
        'configuration_version':snapshot.schema_version,'worker_checked':worker,
        'external_services_checked':False}


def parser():
    result=argparse.ArgumentParser(description='Anime Discovery NextGen local service tools')
    commands=result.add_subparsers(dest='command',required=True)
    for name in ('init','check','worker','config-preview','config-upgrade','database-preview','database-upgrade'):
        command=commands.add_parser(name)
        command.add_argument('--config-dir',required=True)
        if name=='init':
            command.add_argument('--database',required=True,help='New SQLite file on local storage.')
            command.add_argument('--settings-file',help='JSON settings object without credentials.')
        if name=='check':command.add_argument('--worker',action='store_true')
        if name=='config-upgrade':command.add_argument('--fingerprint',required=True)
        if name=='database-upgrade':command.add_argument('--plan-token',required=True)
        if name in ('config-upgrade','database-upgrade'):
            command.add_argument('--services-stopped',action='store_true',required=True,
                help='Assert API/worker are stopped and this installed source is the intended version.')
    return result


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        store=ConfigurationStore(args.config_dir)
        if args.command=='init':
            settings=None
            if args.settings_file:
                with open(args.settings_file,'rb') as stream:settings=decode(stream.read(65537))
                if type(settings) is not dict:raise ValueError('Settings must be a JSON object.')
            output=initialize_local(args.config_dir,args.database,settings)
        elif args.command=='config-preview':output=asdict(store.preview_migration())
        elif args.command=='config-upgrade':
            result=store.migrate(expected_fingerprint=args.fingerprint)
            output={'saved':result.saved,'configuration_version':result.snapshot.schema_version,'durability_confirmed':result.durability_confirmed}
        elif args.command=='database-preview':
            with database_preview(args.config_dir) as (_,plan):output={**asdict(plan),'plan_token':plan_token(plan)}
        elif args.command=='database-upgrade':output=upgrade_database(args.config_dir,args.plan_token)
        elif args.command=='check':output=check_local(args.config_dir,worker=args.worker)
        else:
            check_local(args.config_dir,worker=True)
            from src.nextgen.providers.configuration import run_identity_worker
            asyncio.run(run_identity_worker(
                args.config_dir,backfill=True,
                backfill_select=True,backfill_acquire=True))
            return 0
        print(json.dumps(output,sort_keys=True))
        return 0
    except KeyboardInterrupt:return 130
    except StartupRefused as exc:
        # One clear line per misconfigured required service. The exit code
        # tells systemd not to restart until the settings are fixed.
        print(exc.reason_code+': The worker did not start.',file=sys.stderr)
        for line in exc.lines():print(line,file=sys.stderr)
        return STARTUP_EXIT_CODE
    except Exception as exc:
        # Do not print raw settings, URLs, transport responses or credentials.
        code=getattr(exc,'reason_code','SERVICE_SETUP_FAILED')
        if not isinstance(code,str) or not code.isupper() or not all(c.isalpha() or c=='_' for c in code):code='SERVICE_SETUP_FAILED'
        from src.nextgen.diagnostics import exact_error
        print(code+': Local setup/service operation stopped. Review configuration, local files and maintenance state. A failed initialization may have created some files; existing files are never replaced.',file=sys.stderr)
        print('Cause: '+exact_error(exc),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
