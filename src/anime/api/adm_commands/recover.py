"""adm recover: stopped job diagnostics, classification and safe recovery."""
NAME = 'recover'
SUMMARY = 'Stopped jobs: summary, list, preview, apply, resv, batch-audit'


def main(argv, prog):
    from src.anime.job_recovery import main as recover
    return recover(argv)
