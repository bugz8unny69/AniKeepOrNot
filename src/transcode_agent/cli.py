"""`adm server agent`: the transcode agent's commands.

Normal `adm` and the agent archive (adm-agent.pyz) both call main() here, so
the commands, help and behaviour are the same. Nothing here imports src.anime:
the agent needs no database, settings file or admin socket.
"""
import argparse
import os
import signal
import sys
import threading

from src.transcode_agent import encode, version_text

ENVIRONMENT = """configuration (environment variables, normally from the service's environment file):
  TRANSCODE_LISTEN        host:port to listen on (default 0.0.0.0:8787)
  TRANSCODE_TOKEN         shared bearer token, required; the same token is saved
                          for this agent in AniKeepOrNot under Settings > Transcode
  TRANSCODE_ENCODERS      gpu, cpu or gpu,cpu (default gpu,cpu; GPU is tried first)
  TRANSCODE_VAAPI_DEVICE  VAAPI render node (default /dev/dri/renderD128)
  TRANSCODE_TMP           parent folder; only its transcode-agent/ subfolder is used (default /var/tmp)

host requirements: Python 3.11+, ffmpeg and ffprobe (with libx265 and libopus;
hevc_vaapi for GPU encoding). The agent normally runs as the systemd service
transcode-agent: systemctl status transcode-agent, journalctl -u transcode-agent."""


def settings(environ=os.environ):
    listen = environ.get('TRANSCODE_LISTEN', '0.0.0.0:8787')
    host, _, port = listen.rpartition(':')
    host = host.strip('[]')
    if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('TRANSCODE_LISTEN must be host:port')
    token = environ.get('TRANSCODE_TOKEN', '')
    if not token:
        raise ValueError('TRANSCODE_TOKEN is required')
    return {'host': host, 'port': int(port), 'token': token,
            'encoders': encode.parse_encoders(environ.get('TRANSCODE_ENCODERS', 'gpu,cpu')),
            'vaapi_device': environ.get('TRANSCODE_VAAPI_DEVICE', encode.DEFAULT_VAAPI_DEVICE),
            'tmp': environ.get('TRANSCODE_TMP', '/var/tmp')}


def run(environ=os.environ):
    """Run the HTTP listener until SIGTERM/SIGINT. Returns the exit status."""
    from src.transcode_agent.server import Agent, make_server, reaper
    try:
        values = settings(environ)
    except ValueError as exc:
        print(f'transcode-agent: {exc}', file=sys.stderr)
        return 2
    agent = Agent(tmp=values['tmp'], token=values['token'], encoders=values['encoders'],
                  vaapi_device=values['vaapi_device'])
    server = make_server(agent, values['host'], values['port'])
    stop = threading.Event()
    threading.Thread(target=reaper, args=(agent, stop), daemon=True, name='transcode-reaper').start()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print(f'transcode-agent: {version_text()}, listening on {values["host"]}:{values["port"]}, '
          f'encoders {",".join(values["encoders"])}, files under {agent.folder}', flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        agent.shutdown()
    return 0


def parser(prog='adm server agent'):
    p = argparse.ArgumentParser(prog=prog, description='AniKeepOrNot transcode agent. Without a command, '
                                'runs the HTTP agent (what the transcode-agent service does).',
                                epilog=ENVIRONMENT, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--version', action='store_true', help='print the agent version and build revision')
    commands = p.add_subparsers(dest='command', metavar='command')
    commands.add_parser('run', help='run the HTTP agent in the foreground (the default)')
    return p


def main(argv=None, prog='adm server agent'):
    args = parser(prog).parse_args(argv)
    if args.version:
        print(version_text())
        return 0
    return run()
