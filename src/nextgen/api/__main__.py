"""Explicit API entry point; no auto-initialize, migrate, provider or worker start."""
import argparse
import getpass
import ipaddress
import logging
import re

from src.nextgen.api.app import Application
from src.nextgen.secrets import SecretStore
from src.nextgen.systemd_notify import notify


class TransportFilter(logging.Filter):
    def filter(self,record):
        # Third-party parser/transport errors must not emit a URL/query/header.
        record.msg='API_TRANSPORT_NOTICE: The API transport needs attention.'
        record.args=();record.exc_info=None;record.exc_text=None;record.stack_info=None
        return True


def main():
    parser=argparse.ArgumentParser(description='Anime Discovery NextGen API')
    parser.add_argument('command',choices=('serve','init-access'))
    parser.add_argument('--config-dir',required=True)
    parser.add_argument('--public-origin',help='Exact browser origin; HTTPS behind a trusted proxy, or local loopback HTTP.')
    args=parser.parse_args()
    if args.command=='init-access':
        store=SecretStore(args.config_dir)
        webhook=getpass.getpass('Autobrr webhook key (32–256 URL-safe characters): ')
        if re.fullmatch(r'[A-Za-z0-9_-]{32,256}',webhook) is None:
            parser.error('A URL-safe webhook key of 32–256 characters is required.')
        status=store.status()
        result=(store.initialize(webhook_token=webhook) if status.revision==0
                else store.update_webhook_token(webhook,expected_revision=status.revision))
        print('Webhook key saved.' if result.durability_confirmed else 'Webhook key saved; storage durability needs review.')
        return
    from src.nextgen.__main__ import check_local
    check_local(args.config_dir)
    app=Application(args.config_dir,origin=args.public_origin)
    host=app.startup.value('api.host')
    try:
        if host!='localhost' and not ipaddress.ip_address(host).is_loopback:raise ValueError()
    except ValueError:
        app.close();parser.error('Bind the API to loopback and publish it through a TLS reverse proxy.')
    try:
        import uvicorn
    except ImportError:
        app.close();parser.error('Install requirements-nextgen-api.txt before serving the API.')
    transport=logging.getLogger('uvicorn.error')
    transport.addFilter(TransportFilter())
    try:
        class NotifyingServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                if self.started and not self.should_exit:
                    notify('READY=1')

        config = uvicorn.Config(app, host=host, port=app.startup.value('api.port'),
            workers=1, interface='asgi3', http='h11', ws='none', lifespan='on',
            proxy_headers=False, forwarded_allow_ips='', access_log=False,
            log_level='warning', log_config=None, limit_concurrency=32,
            timeout_keep_alive=5, h11_max_incomplete_event_size=16384,
            server_header=False)
        NotifyingServer(config).run()
    finally:app.close()


if __name__=='__main__':main()
