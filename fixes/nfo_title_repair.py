"""Repair only NextGen-owned published staging NFOs and their durable receipts.

Called under exclusive NextGen maintenance: no worker may race these updates.
After a crash between atomic file publication and SQLite commit, a rerun
reconciles the already-repaired bytes with the previous publication receipt.
"""
import hashlib
import json
import os
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET

from src.anime.acquisition.cleanup import directory, parent
from src.anime.acquisition.media import signature
from src.anime.acquisition.metadata import safe_path
from src.anime.anime_titles import preferred_title
from src.anime.staging import encoded
from src.anime.storage.sqlite import transaction


PUBLICATIONS = """
SELECT 'single', p.plan_id, 0, r.anidb_id, p.manifest_json, p.receipts_json
FROM staging_publications p
JOIN acquisition_plans a USING(plan_id)
JOIN episode_reservations r USING(reservation_id)
WHERE p.state='PUBLISHED'
UNION ALL
SELECT 'batch', p.plan_id, p.file_index, r.anidb_id, p.manifest_json, p.receipts_json
FROM batch_staging_publications p
JOIN batch_episode_reservations r ON r.plan_id=p.plan_id AND r.file_index=p.file_index
WHERE p.state='PUBLISHED'
"""


def relabel(xml_text, role, wanted):
    """Preserve NFO identities/episode fields; replace only one display label."""
    original = xml_text.encode('utf-8')
    root = ET.fromstring(original)
    tag = 'title' if role == 'series' else 'showtitle'
    if root.tag != ('tvshow' if role == 'series' else 'episodedetails'):
        raise ValueError('Unexpected owned NFO type')
    fields = root.findall(tag)
    if len(fields) != 1 or not root.findall('uniqueid'):
        raise ValueError('Unverified NFO metadata')
    if fields[0].text == wanted:
        return original
    fields[0].text = wanted
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def _data(db, configuration, overrides):
    titles = {}
    for aid, canonical, en, romaji in db.execute(
        'SELECT a.anidb_id,a.title,t.english_title,t.romaji_title '
        'FROM identified_anime a LEFT JOIN anime_title_metadata t USING(anidb_id)'):
        if aid in overrides:
            en, romaji = overrides[aid]
        titles[aid] = preferred_title(canonical, en, romaji,
                                       configuration.value('anime.title_language'))
    groups = {}
    publications = {}
    for kind, plan_id, index, aid, manifest, receipts in db.execute(PUBLICATIONS):
        plan, proofs = json.loads(manifest), json.loads(receipts)
        if plan.get('root') != configuration.value('storage.user_evaluation_root'):
            raise ValueError('Published staging root differs from configured staging root')
        entries = plan['entries']
        if len(entries) != 3 or len(proofs) != 3:
            raise ValueError('Unexpected publication layout')
        key = (kind, plan_id, index)
        publications[key] = {'plan': plan, 'receipts': proofs, 'old_manifest': manifest,
                             'old_receipts': receipts, 'changed': False}
        for position, (entry, proof) in enumerate(zip(entries, proofs)):
            if entry['role'] != proof['role'] or entry['path'] != proof['path']:
                raise ValueError('Publication receipt mismatch')
            if entry['role'] not in ('episode', 'series'):
                continue
            safe_path(entry['path'])
            old = entry['text'].encode('utf-8')
            if hashlib.sha256(old).hexdigest() != entry['sha256'] or len(old) != entry['size']:
                raise ValueError('NFO publication content is not authentic')
            desired = relabel(entry['text'], entry['role'], titles[aid])
            path = (plan['root'], entry['path'])
            # An NFS remount can change reported device/inode values without
            # changing a shared NFO. The application owns its path and recorded
            # content; historical filesystem identities are not part of that.
            group = groups.setdefault(path, {'old': old, 'new': desired, 'refs': []})
            if group['new'] != desired or group['old'] != old:
                raise ValueError('Conflicting published series NFO content')
            group['refs'].append((key, position))
    return groups, publications


def _inspect(root_path, relative):
    """Read one previously published NFO using directory FDs, never symlinks."""
    with directory(root_path) as root:
        try:
            with parent(root, relative) as (folder, name):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=folder)
                try:
                    sig = signature(os.fstat(fd))
                    if sig[2] > 65536:
                        raise ValueError('Oversized NFO')
                    data = os.read(fd, sig[2] + 1)
                    if len(data) != sig[2] or signature(os.fstat(fd)) != sig:
                        raise ValueError('NFO changed during inspection')
                    return data, list(sig)
                finally:
                    os.close(fd)
        except FileNotFoundError:
            return None


def _replace(root_path, relative, content, previous):
    with directory(root_path) as root:
        with parent(root, relative) as (folder, name):
            sig = signature(os.stat(name, dir_fd=folder, follow_symlinks=False))
            if list(sig) != previous:
                raise ValueError('NFO changed before replacement')
            temporary = '.ad-title-repair-' + uuid.uuid4().hex + '.tmp'
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=folder)
            try:
                os.fchmod(fd, 0o644)
                view = memoryview(content)
                while view:
                    count = os.write(fd, view)
                    if count <= 0:
                        raise OSError('Failed writing NFO metadata')
                    view = view[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.replace(temporary, name, src_dir_fd=folder, dst_dir_fd=folder)
                os.fsync(folder)
            finally:
                try:
                    os.unlink(temporary, dir_fd=folder)
                except FileNotFoundError:
                    pass
            return list(signature(os.stat(name, dir_fd=folder, follow_symlinks=False)))


def repair(db, configuration, *, apply=False, overrides=None):
    """Return changed/missing counts; repair journal proofs with changed NFOs."""
    groups, publications = _data(db, configuration, overrides or {})
    updates = []
    missing = 0
    unchanged = 0
    for (root, relative), group in sorted(groups.items()):
        current = _inspect(root, relative)
        if current is None:  # Episode NFO removed after Keep/Reject.
            missing += 1
            continue
        existing, current_sig = current
        if group['new'] == group['old']:
            unchanged += 1
            continue
        if existing not in (group['old'], group['new']):
            raise ValueError('NFO has been modified outside NextGen; refusing overwrite')
        updates.append((root, relative, group, current_sig, existing))
    if not apply:
        return {'changed': len(updates), 'missing': missing, 'unchanged': unchanged}
    artifacts = []
    for root, relative, group, before, existing in updates:
        changed_sig = (_replace(root, relative, group['new'], before)
                       if existing != group['new'] else before)
        checksum = hashlib.sha256(group['new']).hexdigest()
        for key, index in group['refs']:
            row = publications[key]
            entry = row['plan']['entries'][index]
            proof = row['receipts'][index]
            if proof['artifact'] is not None:
                artifacts.append((proof['artifact'], changed_sig[0], changed_sig[1]))
            entry.update(text=group['new'].decode('utf-8'), sha256=checksum,
                         size=len(group['new']))
            proof.update(sha256=checksum, file=changed_sig)
            row['changed'] = True
    with transaction(db):
        for (kind, plan_id, index), row in publications.items():
            if not row['changed']:
                continue
            table = 'staging_publications' if kind == 'single' else 'batch_staging_publications'
            identity = 'plan_id=?' if kind == 'single' else 'plan_id=? AND file_index=?'
            args = (plan_id,) if kind == 'single' else (plan_id, index)
            updated = db.execute(
                f'UPDATE {table} SET manifest_json=?, receipts_json=? '
                f'WHERE {identity} AND state=\'PUBLISHED\' AND manifest_json=? AND receipts_json=?',
                (encoded(row['plan']), encoded(row['receipts']), *args,
                 row['old_manifest'], row['old_receipts'])).rowcount
            if updated != 1:
                raise ValueError('Publication changed during NFO repair')
        for artifact, new_dev, new_ino in set(artifacts):
            # Refresh the compatibility receipt without requiring stale NFS
            # inode values to match. Cleanup authorization uses the artifact ID
            # and operation ownership; no file is deleted here.
            updated = db.execute(
                "UPDATE staging_cleanup_artifacts SET file_device=?,file_inode=? "
                "WHERE artifact_id=? AND phase='PUBLISHED'",
                (new_dev, new_ino, artifact)).rowcount
            if updated != 1:
                raise ValueError('NFO artifact proof changed during repair')
    return {'changed': len(updates), 'missing': missing, 'unchanged': unchanged}
