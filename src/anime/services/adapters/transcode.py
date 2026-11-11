"""Transcode-agent health, checked by the existing worker tick.

Each agent gets its own reason, so a wrong token, a missing token, an agent
that cannot be reached and an agent needing an update read differently. Only
reachable agents with this worker's AGENT_VERSION count as alive; an agent
needing an update is shown as such, never as simply offline.
"""
import http.client

from src.anime.services.base import ServiceAdapter, UP, DOWN, DISABLED, NOT_CONFIGURED, AUTH_FAILED, UNSUPPORTED
from src.anime.services.transcode import Remote, TranscodeFailed, agent_name, compatibility


def describe(address, token, remote_factory=None):
    """(kind, text) for one agent: kind is 'up', 'update', 'auth', 'token' or 'down'."""
    name = agent_name(address)
    if not token:
        return 'token', f'{name}: no token set (Settings > Transcode)'
    try:
        health = (remote_factory or Remote)(address, token).health()
        problem = compatibility(address, health)
        if problem:
            return 'update', problem
        return 'up', (f"{name}: up, {'busy' if health['busy'] else 'idle'}, {','.join(health['encoders'])}, "
                      f"version {health['agent_version']}")
    except TranscodeFailed as exc:
        if exc.status in (401, 403):
            return 'auth', f'{name}: token rejected; the saved token must match TRANSCODE_TOKEN on the agent'
        return 'down', f'{name}: answered HTTP {exc.status}' if exc.status else f'{name}: {exc}'[:120]
    except (OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, 'strerror', None) or type(exc).__name__
        return 'down', f'{name}: unreachable ({reason})'
    except (ValueError, KeyError, TypeError):
        return 'down', f'{name}: gave an unreadable health answer'


class TranscodeAdapter(ServiceAdapter):
    name = 'transcode'
    label = 'Transcode agents'
    optional = True

    def probe(self, configuration, secrets, *, now_ms=0):
        if configuration.value('transcode.mode') != 'remote':
            return self.result(DISABLED, now_ms=now_ms)
        agents = configuration.value('transcode.agents')
        if not agents:
            return self.result(NOT_CONFIGURED, 'Agents alive: 0/0. Add an agent under Settings > Transcode.', now_ms=now_ms)
        results = [describe(address, secrets.transcode_token(address)) for address in agents]
        kinds = [kind for kind, _ in results]
        alive = kinds.count('up')
        # UP with a partial count is yellow in the existing bar presentation.
        status = (UP if alive else UNSUPPORTED if 'update' in kinds
                  else AUTH_FAILED if set(kinds) <= {'auth', 'token'} and 'auth' in kinds
                  else NOT_CONFIGURED if set(kinds) == {'token'} else DOWN)
        detail = f'Agents alive: {alive}/{len(agents)}. ' + '; '.join(text for _, text in results)
        return self.result(status, detail[:500], now_ms=now_ms)
