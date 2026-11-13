"""adm agent: the transcode agent (the same commands as adm-agent.pyz)."""
NAME = 'agent'
SUMMARY = 'Transcode agent: run, install, upgrade, rollback, status, token, pack'


def main(argv, prog):
    from src.transcode_agent.cli import main as agent
    return agent(argv, prog=prog)
