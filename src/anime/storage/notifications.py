"""The notification inbox: plain storage functions both notifiers use (work item P3).

Every row is unread; Mark read deletes it. An event with the group_key of a
row already in the inbox merges into it: the count goes up, the title and
message become the latest, and the item moves to the top. At most CAP rows are
kept; the oldest go first.
"""
CAP = 5000
COLUMNS = ('notification_id', 'type', 'severity', 'title', 'message', 'anidb_id', 'job_id',
           'release_name', 'group_key', 'count', 'created_at_ms', 'last_at_ms')


def save(db, event, now):
    """Store one event in the caller's transaction; returns its notification ID."""
    if not db.in_transaction:
        raise ValueError('Transaction required')
    key = event.get('group_key')
    previous = None if key is None else db.execute(
        'SELECT notification_id,count,created_at_ms,job_id,raw_body FROM notifications WHERE group_key=?', (key,)).fetchone()
    count, created, job, raw = 1, now, event.get('job_id'), event.get('raw_body')
    if previous is not None:
        # Re-inserted so the merged item is the newest; it keeps its count and first time.
        db.execute('DELETE FROM notifications WHERE notification_id=?', (previous[0],))
        count, created = previous[1] + 1, previous[2]
        job = job if job is not None else previous[3]
        raw = raw if raw is not None else previous[4]
    number = db.execute(
        'INSERT INTO notifications(type,severity,title,message,anidb_id,job_id,release_name,group_key,raw_body,'
        'count,created_at_ms,last_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (event['type'], event.get('severity', 'info'), event['title'], event['message'], event.get('anidb_id'),
         job, event.get('release_name'), key, raw, count, created, max(now, created))).lastrowid
    db.execute('DELETE FROM notifications WHERE notification_id IN (SELECT notification_id FROM notifications '
               'ORDER BY notification_id DESC LIMIT -1 OFFSET ?)', (CAP,))
    return number


def unread(db):
    return db.execute('SELECT count(*) FROM notifications').fetchone()[0]


def items(db, *, before_id=None, limit=20):
    """Newest first; before_id pages to older items."""
    rows = db.execute(
        'SELECT ' + ','.join(COLUMNS) + ' FROM notifications WHERE notification_id<? '
        'ORDER BY notification_id DESC LIMIT ?', ((1 << 63) - 1 if before_id is None else before_id, limit)).fetchall()
    return [dict(zip(COLUMNS, row)) for row in rows]


def get(db, notification_id):
    row = db.execute('SELECT ' + ','.join(COLUMNS) + ',raw_body FROM notifications WHERE notification_id=?',
                     (notification_id,)).fetchone()
    return None if row is None else dict(zip((*COLUMNS, 'raw_body'), row))


def delete(db, notification_id):
    return db.execute('DELETE FROM notifications WHERE notification_id=?', (notification_id,)).rowcount == 1


def delete_all(db):
    return db.execute('DELETE FROM notifications').rowcount
