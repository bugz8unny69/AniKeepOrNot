"""Keep or Reject, for refusing new releases of a decided series: the series job decides.

The cutover turns every earlier decision (anime_review_decisions) into a series
row, KEPT or REJECTED, so the series row is the only record.
"""


def decision(db, anidb_id):
    """'ACCEPT' (kept, or Keep under way), 'REJECT' (rejected, or Reject under way), or None."""
    row = db.execute("SELECT state,json_type(work_json,'$.keep'),json_type(work_json,'$.reject') "
                     "FROM series WHERE anidb_id=?", (anidb_id,)).fetchone()
    if row is None:
        return None
    if row[0] == 'KEPT' or row[1] is not None:
        return 'ACCEPT'
    if row[0] == 'REJECTED' or row[2] is not None:
        return 'REJECT'
    return None
