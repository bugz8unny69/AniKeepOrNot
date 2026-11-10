"""Run the transcode agent: python -m src.transcode_agent. Configuration is environment only.

  TRANSCODE_LISTEN        host:port to listen on (default 0.0.0.0:8787)
  TRANSCODE_TOKEN         shared bearer token; requests without it get 401 (required)
  TRANSCODE_ENCODERS      gpu, cpu or gpu,cpu: the encoders allowed here (default gpu,cpu)
  TRANSCODE_VAAPI_DEVICE  VAAPI render node (default /dev/dri/renderD128)
  TRANSCODE_TMP           folder whose transcode-agent/ subfolder holds the files (default /var/tmp)
"""
import os
import signal
import sys
import threading

from src.transcode_agent import encode
from src.transcode_agent.server import Agent, make_server, reaper


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


def main():
    try:
        values = settings()
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
    print(f'transcode-agent: listening on {values["host"]}:{values["port"]}, encoders {",".join(values["encoders"])}, '
          f'files under {agent.folder}', flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
