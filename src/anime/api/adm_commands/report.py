"""adm report: read-only diagnostics straight from the database; the API need not run."""
NAME = 'report'
SUMMARY = 'Read-only reports (job, intake)'


def main(argv, prog):
    from src.anime.job_report import main as report
    return report(argv)
