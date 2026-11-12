"""python -m src.transcode_agent: the same as `adm agent` (see cli.py)."""
from src.transcode_agent.cli import main

if __name__ == '__main__':
    raise SystemExit(main(prog='python -m src.transcode_agent'))
