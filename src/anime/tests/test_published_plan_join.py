"""published() must match the staging publication by the acquisition plan.

Regression: the reservation's own plan_id is the *trial* plan. Joining the
publication USING(plan_id) matched that trial plan id instead, so staging
completion saw nothing published and rolled back with
EPISODE_RESERVATION_CONFLICT (Tougen Anki S02E03, job #2875).
"""
import sqlite3
import unittest

from src.anime.storage.episode_reservations import published


class PublishedPlanJoinTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.executescript("""
            CREATE TABLE episode_reservations(reservation_id INTEGER,owner_job_id INTEGER,plan_id INTEGER,state TEXT);
            CREATE TABLE acquisition_plans(plan_id INTEGER,reservation_id INTEGER);
            CREATE TABLE staging_publications(plan_id INTEGER,state TEXT);
            INSERT INTO episode_reservations VALUES(261,2667,23,'ACTIVE');
            INSERT INTO acquisition_plans VALUES(119,261);
        """)

    def test_publication_of_the_acquisition_plan_counts(self):
        self.db.execute("INSERT INTO staging_publications VALUES(119,'PUBLISHED')")
        self.assertTrue(published(self.db,2667))

    def test_planned_or_unrelated_publication_does_not_count(self):
        self.db.execute("INSERT INTO staging_publications VALUES(119,'PLANNED')")
        self.db.execute("INSERT INTO staging_publications VALUES(23,'PUBLISHED')")  # trial plan id
        self.assertFalse(published(self.db,2667))
