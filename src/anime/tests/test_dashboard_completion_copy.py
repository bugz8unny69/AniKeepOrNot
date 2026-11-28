"""Static regression: completed jobs must not imply staging/playback state."""
from pathlib import Path
import unittest


DASHBOARD_JS = (Path(__file__).resolve().parents[1]
                / "dashboard" / "static" / "dashboard.js")


class CompletionCopyTests(unittest.TestCase):
    def test_completed_jobs_do_not_display_generic_staging_warning(self):
        script = DASHBOARD_JS.read_text(encoding="utf-8")
        self.assertNotIn("Staging is unfinished.", script)
        self.assertNotIn("A completed job does not mean the anime is ready to watch.", script)
        self.assertNotIn("dashboard.not_staged", script)

    def test_the_reasons_the_series_job_records_are_shown(self):
        # The Jobs page (and its job reasons) went with the per-step jobs (2026-11-27). What
        # the series job records is shown instead: why a torrent waits or stopped, why a
        # series or its Keep stopped.
        script = DASHBOARD_JS.read_text(encoding="utf-8")
        self.assertIn("whyText(t.why)", script)
        self.assertIn("STAGING_FULL: 'staging is full", script)
        self.assertIn("series.stopped ? 'Stopped: ' + series.stopped", script)
        self.assertIn("series.keep?.stopped ? 'Keep stopped: '", script)

if __name__ == "__main__":
    unittest.main()
