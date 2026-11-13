"""adm server agent: older spelling of `adm agent`, kept because installed units and scripts use it."""
NAME = 'server'
SUMMARY = 'Older spelling of adm agent'
HIDDEN = True


def main(argv, prog):
    if argv[:1] != ['agent']:
        raise SystemExit('usage: adm agent [command] ... (see adm agent --help)')
    from src.transcode_agent.cli import main as agent
    return agent(argv[1:], prog=f'{prog} agent')
