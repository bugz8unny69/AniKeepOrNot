"""adm transcode: transcode staged videos through the worker."""
NAME = 'transcode'
SUMMARY = 'Transcode staged videos (staged, status)'


def main(argv, prog):
    from src.anime.transcode_cli import main as transcode
    return transcode(argv, prog=prog)
