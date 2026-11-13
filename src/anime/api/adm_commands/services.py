"""adm services: one-off check of every external service; AniDB is read from cache."""
NAME = 'services'
SUMMARY = 'Check the external services once (check)'


def main(argv, prog):
    from src.anime.services.check import main as services
    return services(argv)
