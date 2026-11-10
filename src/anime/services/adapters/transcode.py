"""Aggregate optional transcode-agent health, checked by the existing worker tick."""
import http.client
from urllib.parse import urlsplit

from src.anime.services.base import ServiceAdapter, UP, DOWN, DISABLED
from src.anime.services.transcode import Remote, TranscodeFailed


class TranscodeAdapter(ServiceAdapter):
    name = 'transcode'
    label = 'Transcode agents'
    optional = True

    def probe(self, configuration, secrets, *, now_ms=0):
        if configuration.value('transcode.mode') != 'remote':
            return self.result(DISABLED, now_ms=now_ms)
        agents = configuration.value('transcode.agents')
        details, alive = [], 0
        for address in agents:
            host = urlsplit(address).netloc
            try:
                health = Remote(address, secrets.transcode_token(address)).health()
                alive += 1
                details.append(f"{host}: up, {'busy' if health['busy'] else 'idle'}, {','.join(health['encoders'])}")
            except (OSError, ValueError, KeyError, http.client.HTTPException, TranscodeFailed):
                details.append(f'{host}: down')
        # UP with a partial count is yellow in the existing bar presentation.
        status = UP if alive else DOWN
        return self.result(status, (f'Agents alive: {alive}/{len(agents)}. '+'; '.join(details))[:500], now_ms=now_ms)
