"""adm report: read-only diagnostics straight from the database; the API need not run."""
NAME = 'report'
SUMMARY = 'Read-only job reports (job)'


def main(argv, prog):
    from src.anime.job_report import main as report
    return report(argv)
