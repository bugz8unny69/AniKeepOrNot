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

    def test_real_job_reasons_and_actual_staged_state_still_render(self):
        script = DASHBOARD_JS.read_text(encoding="utf-8")
        self.assertIn("if (job.reason)", script)
        self.assertIn("details.appendChild(node('p', job.reason.message))", script)
        self.assertIn("job.cancellation.state === 'STAGED'", script)
        self.assertIn("COPY['dashboard.staged']", script)


if __name__ == "__main__":
    unittest.main()
