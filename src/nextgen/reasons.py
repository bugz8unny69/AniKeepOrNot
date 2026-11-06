"""Stable, presentation-ready operational reasons; no dashboard/runtime dependency.

The worker decides state/retry timing. Descriptions are never evaluated
as workflow instructions. Callers must validate details before persistence.
"""
from dataclasses import dataclass
from types import MappingProxyType
import re

@dataclass(frozen=True)
class Reason:
    code: str
    description: str
    message: str
    action: str
    severity: str
    retryable: bool | None

_ENTRIES = (
    Reason('RELEASE_SEARCH_TIMEOUT','The release search took longer than the allowed time, three times in a row. The details name the search service.','The release search timed out.','Nothing to do yet: the search runs once more in 30 minutes. If it times out again, check the search service and its indexers or raise the search timeout, then press Retry.','ERROR',True),
    Reason('RELEASE_WITHDRAWN','The next release in the ranked list is no longer offered by the search service.','Moving on to the next release.','Nothing to do. The next release in the list is tried.','WARNING',True),
    Reason('EPISODE_DISCOVERY_PLAN_CONFLICT','The saved list of episodes to search no longer matches what this search job expected.','The episode search plan changed and needs review.','Open the job report (adm report job --id followed by the job number) to compare the saved episode list, then press Retry. Nothing was downloaded.','WARNING',False),
    Reason('EPISODE_DISCOVERY_OUTDATED','This episode search stopped under the old Sonarr-based discovery, which Prowlarr search has replaced.','This search ran under the old episode discovery.','Press Search again to search with the current release search.','WARNING',False),
    Reason('WAITING_FOR_DUB','English audio is required, but no release advertises both Japanese and English audio, or every release tried was missing one of them.','Waiting for a release with Japanese and English audio.','Nothing to do. A new Dual Audio release from autobrr starts automatically, or press Search again to check Prowlarr now.','INFO',True),
    Reason('RELEASE_CANDIDATES_EXHAUSTED','Every release found for this episode was tried, and each one failed or could not be downloaded.','All releases for this episode failed.','Check whether a better release exists on your indexers, then press Search again.','WARNING',False),
    Reason('METADATA_ENRICHMENT_REVIEW','The anime library service did not return the expected anime and episode for the saved AniDB IDs. The details name the service.','Extra episode details need review.','Check that the anime library service has this anime and episode, then retry that step.','WARNING',False),
    Reason('DECISION_DOWNLOAD_CLEANUP_COMPLETE','The torrent is gone from qBittorrent and its downloaded files were deleted.','Download cleanup finished.','Nothing to do. Staged files, the library copy and the episode history are kept.','INFO',False),
    Reason('DECISION_DOWNLOAD_CLEANUP_WAITING','Cleanup is waiting for other work on this download to finish, or for qBittorrent to confirm an earlier stop or remove request.','Download cleanup is waiting.','Nothing to do. Files are deleted only after qBittorrent confirms the torrent is gone.','INFO',None),
    Reason('DECISION_DOWNLOAD_CLEANUP_UNAVAILABLE','qBittorrent or the download folder could not be reached.','Download cleanup will try again.','Check that qBittorrent is running and the download folder is mounted. The staged video and the Keep or Reject decision are not affected.','WARNING',True),
    Reason('DECISION_DOWNLOAD_CLEANUP_REVIEW','Cleanup could not confirm that this download belongs to this episode, or that its Keep or Reject step finished.','Download cleanup needs review.','Check the torrent in qBittorrent and the files listed in the job report, then press Retry.','WARNING',False),
    Reason('CANCELED_RESERVATIONS_RELEASED','Cleanup after a cancellation finished, so the episodes this work had reserved are free again. History is kept.','Canceled episodes can be downloaded again.','Nothing to do. A later release can pick these episodes up. Kept episodes stay protected.','INFO',False),
    Reason('POST_KEEP_CLEANUP_COMPLETE','The staged copies were deleted after the library copy was confirmed.','Staged files removed.','Nothing to do. The episode is in the library.','INFO',False),
    Reason('POST_KEEP_CLEANUP_WAITING','Another attempt is still cleaning up the staged files for this kept episode.','Waiting to finish cleaning up staged files.','Nothing to do. The episode is already kept.','INFO',None),
    Reason('POST_KEEP_CLEANUP_UNAVAILABLE','Sonarr, the staging folder or the library folder could not be reached.','The episode is kept; staged-file cleanup will try again.','Check that Sonarr is running and the folders are mounted.','WARNING',True),
    Reason('POST_KEEP_CLEANUP_REVIEW','The saved Keep result or the staged files to delete could not be confirmed.','The episode is kept, but staged-file cleanup needs review.','Check the library file and the staged files, then press Keep again. Only the cleanup runs again.','WARNING',False),
    Reason('MEDIA_SERVER_REFRESH_REVIEW','The media server answered the library scan request in an unexpected way. The details name the server.','The media server library scan needs review.','Check the media server address and that its version is supported.','WARNING',False),
    Reason('KEEP_CONFIGURATION_INVALID','The library manager (address and API key) or the permanent library folder is not set up.','Keep is not set up.','Fix the library manager and library folder settings, then press Keep again.','WARNING',False),
    Reason('KEEP_IMPORT_WAITING','The import into Sonarr is in progress and its result is being checked.','Keeping this episode is in progress.','Nothing to do. Sonarr is checked again shortly.','INFO',None),
    Reason('KEEP_IMPORT_REVIEW','The staged file, its TVDB episode, or the Sonarr import result could not be confirmed.','Keeping this episode needs review.','Check the staged file and the episode in Sonarr, then press Keep again.','WARNING',False),
    Reason('KEEP_COMPLETE','Sonarr imported the file and the library copy was confirmed.','This episode is kept.','Nothing to do. The episode is in the library.','INFO',False),
    Reason('ANIME_REJECTED','Someone rejected this anime, so no more episodes are downloaded for it.','This anime was rejected; downloads are stopped.','Nothing to do. To download it again, review the Reject decision first.','INFO',False),
    Reason('REJECT_CLEANUP_REVIEW','Some staged files of the rejected anime could not be deleted at their recorded paths.','The anime is rejected, but some staged files were not removed.','Check the staged files listed in the Reject job, then press Retry.','WARNING',False),
    Reason('STAGING_REVIEW','The staged copy could not be confirmed: the destination, the copied file or its checksum did not match.','Staging needs review; no files were overwritten.','Check the worker log and the staging paths in the job report, then press Retry. Partial files are kept for recovery.','WARNING',False),
    Reason('STAGING_UNAVAILABLE','A file operation in the staging folder failed, probably temporarily.','The staging folder is unavailable.','Check that the staging folder is mounted, has free space, and is writable by the worker.','ERROR',True),
    Reason('STAGING_CONFIGURATION_INVALID','The staging folder is missing, not writable, overlaps another folder, or the worker is running as root.','The staging folder is not set up correctly.','Set a separate staging folder the worker account can write to, and run the worker as its service account.','WARNING',None),
    Reason('STAGING_BUSY','Another staging step for this episode or series is still running.','Waiting for another staging step to finish.','Nothing to do. It is checked again shortly.','INFO',None),
    Reason('MEDIA_VERIFICATION_INVALID','The downloaded video changed, or it failed the file and container checks.','The downloaded video failed verification.','Check the video file and the release it came from.','WARNING',False),
    Reason('MEDIA_VERIFICATION_UNAVAILABLE','The video could not be read, or checking it took too long.','The downloaded video could not be checked yet.','Check that the download folder is mounted and the file is readable.','ERROR',None),
    Reason('MEDIA_PROBE_UNAVAILABLE','ffprobe could not be started to inspect the video.','The video checker (ffprobe) is not available.','Install ffprobe at /usr/bin/ffprobe and make sure the worker can run it.','WARNING',False),
    Reason('MEDIA_AUDIO_REQUIRED','The video does not have the audio languages required by the Audio check setting (commentary tracks do not count).','The video is missing a required audio language.','Check the audio tracks, or change the Audio check setting. File names and subtitles do not count.','WARNING',False),
    Reason('TORRENT_METADATA_WAITING','qBittorrent is still fetching the torrent file list from peers.','Waiting for the torrent file list.','Nothing to do. It is checked again without adding the torrent twice.','INFO',None),
    Reason('TORRENT_METADATA_TIMEOUT','qBittorrent could not get the torrent file list within ten minutes, usually because the torrent has no peers.','The torrent file list timed out.','Nothing to do. This release is cleaned up and the next one is tried.','WARNING',False),
    Reason('TORRENT_METADATA_DENIED','qBittorrent refused the request for the torrent file list.','qBittorrent refused to provide the torrent file list.','Check the qBittorrent login and version, and the torrent hash.','WARNING',False),
    Reason('MANAGEMENT_JOB_QUEUED','An action for this work is already queued, so another one cannot start.','An action is already queued.','Wait for it to start.','INFO',None),
    Reason('MANAGEMENT_JOB_RUNNING','An action for this work is already running, so another one cannot start.','An action is already running.','Wait for it to finish.','INFO',None),
    Reason('MANAGEMENT_JOB_WAITING','An action for this work is waiting for its next check.','An action is waiting to continue.','Open the job for details.','INFO',None),
    Reason('MANAGEMENT_JOB_ATTENTION','An action for this work stopped and needs someone to look at it.','An action needs attention before it can continue.','Open the job for details.','WARNING',False),
    Reason('MANAGEMENT_JOB_COMPLETE','The action for this work already finished.','This action is already done.','Refresh the page to see what you can do next.','INFO',False),
    Reason('MANAGEMENT_JOB_CANCELED','The action for this work was canceled.','This action was canceled.','Open the job for details.','INFO',False),
    Reason('MANAGEMENT_ACTION_UNAVAILABLE','This action is not allowed for the work in its current state.','This action is not available here.','Refresh the page to see the actions you can use.','WARNING',False),
    Reason('BULK_MANAGEMENT_PARTIAL','Some of the selected jobs did not allow the action. The ones that succeeded stay changed.','Some of the selected actions failed.','Check which jobs failed and retry only those. Nothing was undone.','WARNING',False),
    Reason('MANAGEMENT_JOB_SUBMITTED','The action was accepted and a job was created for it.','Action submitted.','Follow the job for progress.','INFO',None),
    Reason('MANAGEMENT_JOB_EXISTS','This action was already submitted for this work.','This action is already submitted.','Follow the existing job instead of submitting it again.','INFO',False),
    Reason('API_AUTH_REQUIRED','The request had no valid login or key, or had more than one.','Please sign in.','Sign in, or send the key that this API address requires.','WARNING',False),
    Reason('API_AUTH_UNCONFIGURED','The access key for this API address has not been set up.','API access is not set up.','Set up the webhook and management access keys on the server.','WARNING',False),
    Reason('API_CSRF_REQUIRED','The browser request was missing its security token.','This request could not be verified.','Refresh the page and try again.','WARNING',False),
    Reason('API_ORIGIN_INVALID','The request came from an address other than the configured dashboard address.','This request came from an unexpected address.','Open the dashboard at its configured address.','WARNING',False),
    Reason('MANUAL_RELEASE_EPISODE_MISMATCH','A release submitted by hand names a different episode, in its name or in its video file, than the episode it was submitted for.','This release is for a different episode.','Check the episode in the release name. A release can only be submitted from its own episode. Nothing was downloaded.','WARNING',False),
    Reason('MANUAL_RELEASE_TITLE_UNREADABLE','No season and episode could be read from the name of a release submitted by hand.','The release name does not say which episode it is.','Paste the full release name, including its season and episode (for example S01E05). Nothing was downloaded.','WARNING',False),
    Reason('MANUAL_RELEASE_SOURCE_INVALID','The magnet link or .torrent file submitted by hand is not valid.','The magnet link or torrent file is not valid.','Paste a magnet link that contains its info hash (xt=urn:btih:...), or choose a valid .torrent file. Nothing was downloaded.','WARNING',False),
    Reason('API_REQUEST_INVALID','The request was malformed or had missing or invalid fields.','The request is not valid.','Check the request fields.','WARNING',False),
    Reason('API_WEBHOOK_INVALID','The incoming release is missing its name or torrent hash, or the hash is not valid.','The incoming release details are incomplete.','Send the release name and its 40-character torrent hash.','WARNING',False),
    Reason('API_BODY_TOO_LARGE','The request is larger than allowed.','The request is too large.','Send a smaller request.','WARNING',False),
    Reason('API_MEDIA_TYPE_INVALID','The request was not UTF-8 JSON.','This request format is not supported.','Send UTF-8 JSON.','WARNING',False),
    Reason('API_ROUTE_NOT_FOUND','The API address or the requested item does not exist.','Not found.','Check the address and the item ID.','INFO',False),
    Reason('API_METHOD_INVALID','This API address does not support that kind of request.','This action is not supported here.','Use the request method this address documents.','WARNING',False),
    Reason('API_RATE_LIMITED','Too many sign-in attempts or sessions in a short time.','Too many requests; try again shortly.','Wait a moment, then try again.','WARNING',None),
    Reason('API_UNAVAILABLE','The request failed on the server. Details are in the server log, not shown here.','The server could not complete the request.','Check the service status and the API log, then try again.','ERROR',None),
    Reason('JOB_CANCELED','Someone canceled this work before it finished.','This work was canceled.','Nothing to do. Check the cleanup status if a download was in progress.','INFO',False),
    Reason('CANCELLATION_DRAINING','The cancellation is saved; cleanup starts once the work that is running now has stopped.','Waiting for running work to stop before cleanup.','Nothing to do.','INFO',None),
    Reason('CANCELLATION_PENDING','Cleanup is not finished: stopping or removing the torrent has not been confirmed yet. It is retried after checking the torrent still belongs to this work.','Cancellation cleanup is waiting for confirmation.','Nothing to do unless this continues; then check the torrent in qBittorrent.','INFO',None),
    Reason('CANCELLATION_REVIEW','Cleanup could not safely confirm that a file or torrent belongs to this work, so nothing was deleted.','Cancellation cleanup needs attention.','The failed check and the file involved are shown in History. Run sudo adm report job --id followed by the job number for the full picture, fix or remove only that file, then press Retry. Never delete the whole staging or download folder.','WARNING',False),
    Reason('CANCELLATION_COMPLETE','The torrent and temporary files owned by this work were cleaned up, or there were none.','Cancellation cleanup finished.','Nothing to do.','INFO',None),
    Reason('TORRENT_SELECTION_UNCERTAIN','qBittorrent was asked to download only the chosen file; the result has not been confirmed yet.','Waiting to confirm which file is downloaded.','Nothing to do. The request is not repeated automatically.','INFO',None),
    Reason('TORRENT_SELECTION_CONFIRMED','qBittorrent confirmed that only the chosen video is selected and other files are skipped.','The video file is selected.','Nothing to do. The download starts next.','INFO',None),
    Reason('TORRENT_PROBE_STOP_UNCERTAIN','The first episode finished, and qBittorrent was asked to pause the torrent; the pause has not been confirmed yet.','Waiting to confirm the torrent is paused.','Nothing to do. The pause request is not repeated automatically.','INFO',None),
    Reason('TORRENT_START_UNCERTAIN','qBittorrent was asked to start the download; the start has not been confirmed yet.','Waiting to confirm the download started.','Nothing to do. The start request is not repeated automatically.','INFO',None),
    Reason('TORRENT_DOWNLOAD_IN_PROGRESS','qBittorrent is downloading the chosen video.','Downloading.','Nothing to do.','INFO',None),
    Reason('TORRENT_DOWNLOAD_COMPLETE','qBittorrent reports the chosen video is fully downloaded.','Download finished.','Nothing to do. The video is verified next.','INFO',None),
    Reason('TORRENT_DOWNLOAD_WAITING','The torrent added for this work does not appear in qBittorrent.','Waiting for the torrent to appear in qBittorrent.','Check qBittorrent. Do not add the torrent by hand; it is not added again automatically.','INFO',None),
    Reason('TORRENT_TRANSFER_CONFLICT','The torrent in qBittorrent no longer matches what was recorded: its owner tag, selected files, pieces or state changed.','The download changed and needs review.','Check the torrent and its selected files in qBittorrent before pressing Retry.','WARNING',False),
    Reason('TORRENT_PIECE_OVERLAP','The chosen video shares torrent pieces with files that are not wanted, so it cannot be downloaded alone.','This video cannot be downloaded without other files.','Use a different release, ideally one with only the video.','WARNING',False),
    Reason('TORRENT_SNAPSHOT_CHANGED','The torrent changed while it was being checked, or qBittorrent is checking or moving it.','The download is being checked or moved.','Nothing to do. It is checked again shortly.','INFO',None),
    Reason('IDENTITY_INCOMPLETE','Some details needed to identify the anime are missing.','Anime identification is incomplete.','Check the missing details in the job report, or match the anime by hand.','WARNING',None),
    Reason('IDENTITY_INVALID','The anime that was found failed validation.','The anime could not be verified.','Check the identification results, or match the anime by hand.','WARNING',False),
    Reason('IDENTITY_CONFLICT','The anime IDs that were found disagree with the ones already saved.','Conflicting anime information needs review.','Compare the AniDB and TVDB IDs in the job report, then match the anime by hand.','WARNING',False),
    Reason('TORRENT_SUBMISSION_UNCERTAIN','The torrent was sent to qBittorrent, but it has not been confirmed yet. It is never sent twice.','Waiting to confirm the torrent was added.','Nothing to do. Check qBittorrent if this continues; do not add the torrent by hand.','INFO',None),
    Reason('TORRENT_ALREADY_PRESENT','qBittorrent already had this torrent before this work tried to add it.','This torrent is already in qBittorrent; nothing new was added.','Check who added the existing torrent before pressing Retry.','WARNING',False),
    Reason('TORRENT_OWNERSHIP_CONFLICT','The torrent in qBittorrent does not match what this work added: its owner tag, folder, category, state or files differ.','The torrent no longer matches what was added and needs review.','Compare the torrent in qBittorrent with the job report, then press Retry.','WARNING',False),
    Reason('TORRENT_STOPPED_CONFIRMED','qBittorrent confirmed the torrent was added, paused, with the expected files.','The torrent is added and paused.','Nothing to do. The video file is selected next.','INFO',None),
    Reason('DOWNLOAD_CONFIGURATION_INVALID','The qBittorrent address, login or download folder is missing or not valid.','qBittorrent is not set up.','Check the qBittorrent settings and the download folder.','WARNING',False),
    Reason('ACQUISITION_SIZE_LIMIT','The video is larger than the maximum size allowed per episode.','The video is larger than the episode size limit.','Raise "Maximum video size" in Settings, or wait for a smaller release.','WARNING',False),
    Reason('ACQUISITION_PLAN_SIZE_LIMIT','Adding this video would go over the total size allowed for the episodes picked for your evaluation of this anime.','The evaluation episodes for this anime would go over their total size limit.','Raise "Maximum selection-plan size" in Settings.','WARNING',False),
    Reason('ACQUISITION_SIZE_EVIDENCE_MISSING','An earlier download plan for this anime has no recorded file size.','An earlier download plan is missing its file size.','Re-check that earlier release before adding more episodes.','WARNING',False),
    Reason('ACQUISITION_SOURCE_MISSING','There is no supported torrent hash for this release.','This release has no usable torrent.','Check the incoming release.','WARNING',False),
    Reason('ACQUISITION_METADATA_INVALID','The torrent file list could not be verified for the expected episode.','The torrent file list could not be verified.','Check the release and its episode number.','WARNING',False),
    Reason('ACQUISITION_PLAN_CONFLICT','The episode reservation or the release changed after the download was planned.','Download details changed and need review.','Check the episode reservation and the release in the job report.','WARNING',False),
    Reason('ACQUISITION_PLAN_READY','The torrent file list was checked and the download is planned.','The download is planned.','Nothing to do. The torrent is added next.','INFO',None),
    Reason('EPISODE_RESERVATION_GRANTED','This episode is reserved for this work, so no other release downloads it at the same time.','This episode is reserved.','Nothing to do. The release is verified next.','INFO',None),
    Reason('EPISODE_ALREADY_STAGED','Another release of this episode is already staged, so this one was canceled.','Canceled: this episode is already staged.',"Nothing to do. Only this release's own files are cleaned up; the staged episode stays.",'INFO',False),
    Reason('EPISODE_RESERVED','Another job already reserved this episode.','Another job is handling this episode.','Check the other job before resuming this one.','INFO',None),
    Reason('EPISODE_RESERVATION_CONFLICT','The episode details or its reservation do not match each other.','The episode reservation needs review.','Check the episode and its reservation in the job report.','WARNING',False),
    Reason('USER_EVALUATION_SELECTED','This episode is one of the first episodes picked so you can evaluate this anime.','This episode is picked for your evaluation.','Nothing to do. It downloads once its checks pass.','INFO',None),
    Reason('USER_EVALUATION_OUTSIDE_WINDOW','This episode is outside the episodes picked for you to evaluate this anime.','This episode is not picked for your evaluation.','Nothing to do. To pick more episodes, raise the evaluation episode counts in Settings.','INFO',False),
    Reason('USER_EVALUATION_DISABLED','Both evaluation episode counts are zero, so no episodes are picked for new anime.','Picking episodes for evaluation is turned off.','Set the "backfill" or "following" evaluation episodes above zero in Settings.','INFO',False),
    Reason('RELEASE_AUDIO_UNTAGGED','English audio is required, and this autobrr release title does not mention Dual or Multi Audio.','This release is skipped; its episode is searched in Prowlarr instead.','Nothing to do. To download such releases directly, set Audio check to "japanese" or "off".','INFO',None),
    Reason('USER_EVALUATION_PLAN_CONFLICT','The verified episode does not match the saved list of episodes picked for evaluation.','The evaluation episode list needs review.','Check the verified episode and the saved evaluation list in the job report.','WARNING',False),
    Reason('RELEASE_UNRESOLVED','The release does not clearly say which episode it is.','The episode could not be confirmed, so the job stopped.','Press Retry to check again; this records which check failed.','WARNING',False),
    Reason('RELEASE_TITLE_UNCLEAR','The release title does not name exactly one regular episode.','The release title does not clearly name one episode.','Check the episode number in the release title. This release cannot be downloaded until one episode is clear.','WARNING',False),
    Reason('RELEASE_TYPE_UNSUPPORTED','The release is a batch, an episode range, a movie or another unsupported type.','This type of release is not supported.','Check what the release contains. A show whose name includes "Special" is not itself a special.','WARNING',False),
    Reason('RELEASE_SPECIAL_UNSUPPORTED','This is a TVDB Season 0 special, and Anime-Lists has no mapping that links it to an AniDB episode.','This special has no AniDB-to-TVDB episode mapping.','Check Anime-Lists for a mapping for this special. AniDB special numbers are not assumed to match TVDB.','WARNING',False),
    Reason('RELEASE_MAPPING_MISSING','Anime-Lists has no single entry for this anime, so its episodes cannot be matched to TVDB.','Anime-Lists has no entry for this anime.','Check the Anime-Lists entry for this anime, then press Retry.','WARNING',False),
    Reason('RELEASE_MAPPING_UNSUPPORTED','The Anime-Lists episode-numbering rule for this anime cannot be read safely.',"This anime's episode numbering in Anime-Lists is not supported.",'Check the Anime-Lists mapping. The job stopped rather than guess the episode.','WARNING',False),
    Reason('RELEASE_ANIDB_EPISODE_MISSING','AniDB does not list this episode number for the anime.','AniDB does not have this episode.','Check the episode number in the release and the AniDB episode list. Press Retry once AniDB lists it.','WARNING',False),
    Reason('RELEASE_EPISODE_MAPPING_MISSING','Anime-Lists does not link this episode between AniDB and TVDB.','This episode is not linked between AniDB and TVDB.','Check the episode mapping in Anime-Lists, then press Retry once it is added.','WARNING',False),
    Reason('RELEASE_EPISODE_MAPPING_AMBIGUOUS','More than one AniDB episode matches this TVDB season and episode.','More than one episode matches.','Check the episode mapping in Anime-Lists. The job stopped rather than guess.','WARNING',False),
    Reason('RELEASE_TVDB_EPISODE_MISSING','TVDB does not list the season and episode that Anime-Lists points to.','TVDB does not have this season and episode.','Compare the Anime-Lists mapping with TVDB, then press Retry once either is fixed.','WARNING',False),
    Reason('RELEASE_SELECTION_CONFLICT','The saved list of releases for this episode no longer matches what this job expected, for example because another job changed it.','The release choice for this episode needs review.','Open the job report (adm report job --id followed by the job number) to compare the saved releases, then press Retry.','WARNING',False),
    Reason('SEASON_PACK_SELECTION_CONFLICT','The saved list of season packs for this season no longer matches what this job expected, for example because another job changed it.','The season pack choice for this season needs review.','Open the job report (adm report job --id followed by the job number) to compare the saved season packs, then press Retry.','WARNING',False),
    Reason('RELEASE_CONFLICT','The episode mapping or the saved release details disagree.','The release episode details disagree.','Check the episode IDs and mapping in the job report.','WARNING',False),
    Reason('SECRET_STORAGE_UNAVAILABLE','The private credentials file could not be read or saved safely.','Saved credentials could not be read.','Check secrets.json: its owner, its permissions (0600) and its format.','ERROR',None),
    Reason('JOB_LEASE_EXPIRED','The worker stopped or lost contact while this job was running.','Processing was interrupted.','Nothing to do; the job is tried again. If it keeps happening, check the worker log.','WARNING',True),
    Reason('JOB_HANDLER_FAILED','The job failed with an unexpected error.','An unexpected error occurred.','Check the failed check and the worker log, then press Retry.','ERROR',None),
    Reason('JOB_RETRY_EXHAUSTED','The job failed as many times as allowed, so automatic retries stopped.','Automatic retries stopped.','Check the earlier failure reason in History, fix it, then press Retry.','ERROR',False),
    Reason('SERVICE_UNAVAILABLE','A service this job needs could not be reached, was too busy, or kept failing. The details name the service. The job waits and tries again by itself, without using up its retries.','Waiting for a service to come back.','Nothing to do for a short outage: waiting jobs continue on their own. If it lasts, check the service named in the details. After 72 hours the job stops for review.','WARNING',True),
    Reason('SERVICE_RESPONSE_INVALID','A service sent back a response that could not be read or did not make sense. The details name the service.','A service sent a response that could not be used.','Check that the service named in the details is working and up to date, then press Retry.','WARNING',None),
    Reason('SERVICE_SETUP_INVALID','A service is not set up correctly: its address, login, client name or API key is missing or was rejected, or its version does not support what this needs. The details name the service and what is wrong. Waiting would not fix this.','A service is not set up correctly.','Fix the service named in the details in Settings (adm services check tests every service), then press Retry. For a required service, start the worker again.','ERROR',False),
    Reason('STORAGE_UNAVAILABLE','The database could not be read or written. Whether the last change was saved is unknown.','The database is unavailable.','Check the database file, its disk and its permissions.','ERROR',None),
    Reason('STORAGE_STATEMENT_INVALID','A database query in the program is wrong, for example it has the wrong number of values. This is a bug, not a database problem. Nothing was saved.','A program error stopped processing.','Report the failed check from the log. Retrying will fail the same way until the program is fixed.','ERROR',False),
    Reason('UNSUPPORTED_JOB_KIND','No part of the program handles this type of job.','This type of job cannot be processed.','Check that the installed version matches the database.','ERROR',False),
    Reason('USER_EVALUATION_COUNTS_INVALID','Both evaluation episode counts are zero.','Set at least one evaluation episode count above zero.','Raise the "backfill" or "following" evaluation episodes in Settings.','WARNING',False),
    Reason('CONFIGURATION_INVALID','A setting is missing, unknown, or has a value that is not allowed.','The settings are not valid.','Check the settings shown in the error.','WARNING',False),
    Reason('CONFIGURATION_CONFLICT','The settings were changed somewhere else while this change was being made.','The settings changed before this update was saved.','Reload the settings, check the changes, and save again.','WARNING',False),
    Reason('CONFIGURATION_MIGRATION_REQUIRED','The settings file is from an older version and must be upgraded first.','The settings need an upgrade.','Preview and apply the settings upgrade.','WARNING',False),
    Reason('CONFIGURATION_STORAGE_FAILED','The settings file could not be read or saved.','Settings could not be read or saved.','Check the settings folder permissions and disk.','ERROR',None),
    Reason('CONFIGURATION_DURABILITY_UNCONFIRMED','The new settings were written, but the disk did not confirm they are safely stored.','Settings were saved, but the disk did not confirm it.','Reload the settings to check them, and check the disk, before saving again.','ERROR',None),
    Reason('MAINTENANCE_PENDING','Maintenance was requested, so no new work starts.','New work is paused for maintenance.','Wait for maintenance to finish.','INFO',None),
    Reason('MAINTENANCE_BUSY','Work that is still running, or another maintenance task, is holding the database.','Waiting for running work to finish.','Wait, then check the maintenance status.','INFO',None),
    Reason('MAINTENANCE_FAILED','The maintenance state is unclear or could not be saved.','Maintenance needs review.','Check the maintenance status before resuming work.','ERROR',None),
    Reason('DATABASE_VERSION_UNSUPPORTED','The database is from an unknown, newer, or unsupported version.','This database version cannot be upgraded by this version.','Check that you installed the right version and opened the right database.','ERROR',False),
    Reason('DATABASE_SCHEMA_INVALID','The database structure or its integrity checks failed.','The database structure does not match this version.','Leave maintenance on and check the database, or restore a backup.','ERROR',False),
    Reason('DATABASE_MIGRATION_CONFLICT','The database or the upgrade files changed after the upgrade was previewed.','The database upgrade plan changed.','Preview the upgrade again before applying it.','WARNING',False),
    Reason('DATABASE_MIGRATION_FAILED','The upgrade or its backup failed. Some upgrade steps may already be applied.','The database upgrade stopped; maintenance is still on.','Check the current database version and the backup before continuing.','ERROR',None),

    Reason('JOB_WAITING','The job is waiting for something it needs, such as another job or an outside service.','Waiting for something this job needs.','Nothing to do. Open the job to see what it is waiting for.','INFO',None),
    Reason('JOB_STATE_INVALID',"The job's saved progress does not add up, so it was stopped.","This job's progress needs review.",'Check the job history, then press Retry.','ERROR',False),
    Reason('JOB_PAYLOAD_INVALID',"The job's saved input is not valid or is too large.","This job's input is not valid.",'Check the job in the report. It usually needs to be recreated rather than retried.','ERROR',False),
    Reason('WORKER_NOT_READY','The worker started without any job handlers installed.','The worker is not ready.','Reinstall or upgrade the application, then restart the worker.','ERROR',False),

)
REGISTRY = MappingProxyType({item.code: item for item in _ENTRIES})
if len(REGISTRY) != len(_ENTRIES):
    raise ValueError("duplicate reason code")

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}", re.ASCII)
_MAX_INTEGER = (1 << 63) - 1
_CHECK = re.compile(r"src/nextgen/[a-z0-9_/]{1,120}\.py:[1-9][0-9]{0,5} [A-Za-z_][A-Za-z0-9_]{0,63}( [A-Z0-9_]{1,32})?")
_FIELDS = frozenset({
    "schema_version", "anime.anidb_id", "anime.tvdb_series_id", "anime.title",
    "anime.english_title", "anime.original_title", "anime.type", "anime.year",
    "episodes", "episodes[].anidb_episode_id", "episodes[].anidb_episode_number",
    "episodes[].title", "episodes[].type", "episodes[].tvdb_placement",
    "episodes[].tvdb_placement.season", "episodes[].tvdb_placement.episode",
    "attributes",
})


def _code_or_unknown(code) -> str:
    # No arbitrary values, control characters, or unbounded historical strings.
    return code if type(code) is str and _CODE.fullmatch(code) else "UNKNOWN"

def get_reason(code: str) -> Reason | None:
    """Unknown historical codes are allowed and displayed generically."""
    return REGISTRY.get(code) if type(code) is str else None

def display_reason(code: str) -> dict:
    entry = get_reason(code)
    if entry is None:
        return {"code": _code_or_unknown(code),
                "message": "Additional processing information is available.",
                "action": "Review the job history.", "severity": "WARNING",
                "retryable": None}
    return {"code": entry.code, "message": entry.message,
            "action": entry.action, "severity": entry.severity,
            "retryable": entry.retryable}

def safe_details(*, missing_fields=(), invalid_fields=(), retry_at_ms=None,
                 attempt=None, max_attempts=None, cause_reason_code=None,
                 video_mb=None, limit_mb=None, plan_used_mb=None,
                 failed_check=None, failed_path=None,
                 delete_cache=None, cache_entries_deleted=None, cached_replay=None,
                 observed_audio=None, missing_audio=None,
                 blocking_job_id=None, operation_id=None,
                 service=None, service_detail=None, service_status=None, waiting_since_ms=None) -> dict:
    """Build bounded history facts; never accept raw values or exception text.

    Field names identify schema locations, not missing episode coverage.
    Counters describe observations, not retry policy. Cause codes must be known.
    """
    details = {}
    for key, values in (("missing_fields", missing_fields),
                        ("invalid_fields", invalid_fields)):
        if type(values) not in (list, tuple) or len(values) > 20:
            raise ValueError("invalid diagnostic field list")
        if any(type(name) is not str or name not in _FIELDS for name in values):
            raise ValueError("invalid diagnostic field")
        if values:
            details[key] = sorted(set(values))
    for key, value in (("retry_at_ms", retry_at_ms), ("attempt", attempt),
                       ("max_attempts", max_attempts), ("video_mb", video_mb),
                       ("limit_mb", limit_mb), ("plan_used_mb", plan_used_mb),
                       ("blocking_job_id", blocking_job_id), ("operation_id", operation_id),
                       ("waiting_since_ms", waiting_since_ms)):
        if value is not None:
            if type(value) is not int or not 0 <= value <= _MAX_INTEGER:
                raise ValueError("invalid diagnostic integer")
            details[key] = value
    for key, value in (("delete_cache", delete_cache), ("cached_replay", cached_replay)):
        if value is not None:
            if type(value) is not bool:
                raise ValueError("invalid diagnostic boolean")
            details[key] = value
    if cache_entries_deleted is not None:
        if type(cache_entries_deleted) is not int or not 0 <= cache_entries_deleted <= _MAX_INTEGER:
            raise ValueError("invalid cache cleanup count")
        details["cache_entries_deleted"] = cache_entries_deleted
    # Code location of the check that stopped the work, e.g.
    # "src/nextgen/cancel_cleanup.py:80 remove_staging EACCES".
    if failed_check is not None:
        if type(failed_check) is not str or not _CHECK.fullmatch(failed_check):
            raise ValueError("invalid failed check")
        details["failed_check"] = failed_check
    # The local file the failed check was looking at (absolute, no controls).
    if failed_path is not None:
        if (type(failed_path) is not str or not failed_path.startswith("/")
                or len(failed_path.encode("utf-8", "surrogateescape")) > 1024
                or any(ord(c) < 32 or ord(c) == 127 for c in failed_path)):
            raise ValueError("invalid failed path")
        details["failed_path"] = failed_path
    if observed_audio is not None:
        if type(observed_audio) is not str or len(observed_audio) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in observed_audio):
            raise ValueError("invalid observed audio tags")
        details["observed_audio"] = observed_audio
    if missing_audio is not None:
        if type(missing_audio) is not str or len(missing_audio) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in missing_audio):
            raise ValueError("invalid missing audio tags")
        details["missing_audio"] = missing_audio
    # The external service involved, by registry name, and a readable status
    # line such as "Prowlarr is unavailable: HTTP 503 at http://...".
    if service is not None:
        if type(service) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", service):
            raise ValueError("invalid service name")
        details["service"] = service
    if service_detail is not None:
        if (type(service_detail) is not str or not service_detail or len(service_detail) > 300
                or any(ord(c) < 32 or ord(c) == 127 for c in service_detail)):
            raise ValueError("invalid service detail")
        details["service_detail"] = service_detail
    # For SERVICE_SETUP_INVALID: which setup problem it is.
    if service_status is not None:
        if service_status not in ("NOT_CONFIGURED", "AUTH_FAILED", "UNSUPPORTED"):
            raise ValueError("invalid service status")
        details["service_status"] = service_status
    if cause_reason_code is not None:
        cause = get_reason(cause_reason_code)
        if cause is None:
            raise ValueError("unregistered cause reason")
        details["cause_reason_code"] = cause.code
    return details

