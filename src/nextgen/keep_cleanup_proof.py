"""Prove a staged Keep and its current permanent Sonarr destination.

Both single-episode and batch cleanup use these checks before authorizing
an exact staged-file unlink. Different journals may supply the same proof.
"""
import json

from src.nextgen.keep_selection import KeepSelection
from src.nextgen.sonarr import KeepReview, existing_file


def staged_keep(manifest, receipts, data, plan_id, *, copy_only=False):
    """Validate all staged artifacts against the durable completed Keep."""
    try:
        selection=KeepSelection(**data['selection'])
        plan=json.loads(manifest)
        proofs=json.loads(receipts)
        entries=plan['entries']
        if (type(entries) is not list or type(proofs) is not list
                or len(entries)!=3 or len(proofs)!=3
                or {entry['role'] for entry in entries}!=
                   {'media','episode','series'}
                or any(entry['role']!=proof['role'] or
                       entry['path']!=proof['path']
                       for entry,proof in zip(entries,proofs))):
            raise KeepReview()
        media=[proof for proof in proofs if proof['role']=='media']
        if (len(media)!=1
                or plan_id!=selection.preparation_plan_id
                or plan['root']!=selection.staging_root
                or plan['root_identity']!=data['root_identity']
                or media[0]['path']!=selection.media_relative_path
                or media[0]['file']!=data['source_file']
                or media[0]['root']!=data['source_parent']):
            raise KeepReview()
        if copy_only and plan.get('transfer')!='COPY':
            raise KeepReview()
        return selection
    except (KeyError,TypeError,ValueError,IndexError):
        raise KeepReview() from None


def permanent_keep(client, selection, options, data):
    """Re-read Sonarr's current episode binding and local file on each guard."""
    try:
        series=client.series(selection.tvdb_series_id)
        if (series is None or
                (series.get('id'),series.get('path'))!=
                (data['series_id'],data['series_path'])):
            raise KeepReview()
        episode=client.episode(selection,series)
        file_id=data['result']['episode_file_id']
        if (episode.get('id')!=data['episode_id']
                or episode.get('hasFile') is not True
                or episode.get('episodeFileId')!=file_id):
            raise KeepReview()
        current=client.request('episodefile/'+str(file_id))
        if existing_file(options,series,episode,current,
                         data['source_file'][2])!=data['result']:
            raise KeepReview()
    except (KeyError,TypeError,ValueError,IndexError):
        raise KeepReview() from None
