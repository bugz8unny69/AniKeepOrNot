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
    Reason('PROWLARR_CONFIGURATION_INVALID','The Prowlarr address or API key is missing or invalid.','Configure Prowlarr before backfill search.','Set the Prowlarr address and API key in Settings, then retry; no download occurred.','WARNING',False),
    Reason('PROWLARR_AUTH_FAILED','Prowlarr rejected the configured API key.','Prowlarr search authentication failed.','Check the Prowlarr API key in Settings, then retry.','WARNING',False),
    Reason('PROWLARR_SEARCH_REVIEW','Prowlarr returned a response that could not be read safely.','Prowlarr search needs review.','Check Prowlarr and its indexers, then retry.','WARNING',False),
    Reason('PROWLARR_SEARCH_TIMEOUT','A Prowlarr search exceeded the configured timeout three times in a row.','Prowlarr search timed out.','The search runs again once after 30 minutes. If it still times out, check Prowlarr and indexer response times or raise the timeout, then retry.','ERROR',True),
    Reason('PROWLARR_UNAVAILABLE','Prowlarr could not be reached, was busy, or failed the search three times in a row.','Prowlarr search failed.','The search runs again once after 30 minutes. If it still fails, check that Prowlarr and its indexers are running, then retry.','ERROR',True),
    Reason('PROWLARR_RELEASE_WITHDRAWN','The next ranked torrent is no longer returned by Prowlarr.','Trying the next ranked release.','No action needed; the next ranked release is tried without a new torrent being sent.','WARNING',True),
    Reason('EPISODE_CONTEXT_CONFLICT','Saved selection, recognized episode or verified release disagree on TVDB placement.','Saved episode identity needs review.','Inspect the conflicting saved placements before retrying; a fresh provider catalog must not override this conflict.','WARNING',False),
    Reason('SONARR_BACKFILL_PLAN_CONFLICT','Frozen backfill scope or stored search receipt did not match the worker plan.','Backfill discovery needs review.','Check the immutable episode window and persisted discovery receipts; no download occurred.','WARNING',False),
    Reason('SONARR_DISCOVERY_EPISODE_NOT_FOUND','The requested TVDB series is absent from Sonarr; its release API requires existing catalog records.','Sonarr cannot search this series.','No series was created. Check the series mapping; this search source requires existing Sonarr records.','ERROR',False),
    Reason('SONARR_DISCOVERY_AUTH_FAILED','Sonarr rejected the configured API key.','Sonarr search authentication failed.','Check the existing Sonarr API key, then resume discovery.','WARNING',False),
    Reason('SONARR_DISCOVERY_UNSUPPORTED','The connected Sonarr server is not a supported v4 instance.','Sonarr discovery requires v4.','Check Sonarr version and the configured address.','WARNING',False),
    Reason('SONARR_DISCOVERY_CONFIGURATION_INVALID','The Sonarr address, API key, quality profile or library root folder is missing or not accepted by Sonarr.','Configure Sonarr before backfill discovery.','Check the Sonarr address and API key, set a Sonarr quality profile ID, and make the library root match a Sonarr root folder, then retry; no download occurred.','WARNING',False),
    Reason('SONARR_DISCOVERY_REVIEW','Sonarr interactive search returned ambiguous or invalid mapping or release evidence.','Sonarr discovery needs review.','Inspect Sonarr release mapping and indexer responses, then retry.','WARNING',False),
    Reason('SONARR_DISCOVERY_TIMEOUT','The Sonarr request exceeded its transport timeout; release searches allow 180 seconds.','Sonarr search timed out.','No automatic retry was scheduled. Check Sonarr and indexer response times, then retry manually.','ERROR',False),
    Reason('SONARR_DISCOVERY_UNAVAILABLE','Sonarr or its interactive search could not complete the request.','Sonarr discovery failed.','Check Sonarr and indexer availability, then retry manually.','ERROR',False),
    Reason('SONARR_EPISODES_NOT_LISTED','Sonarr returned no matching episode record for one or more requested backfill episodes.','Sonarr cannot search the requested episodes.','Check the worker log for the TVDB series, season and missing episode numbers. No refresh or automatic retry was scheduled.','ERROR',False),
    Reason('SONARR_DISCOVERY_NO_RESULTS','The previously ranked fallback torrent is not currently offered by Sonarr.','Waiting for the next ranked Sonarr release.','Check the next candidate in Sonarr; the worker will try again without submitting a torrent.','WARNING',True),
    Reason('WAITING_FOR_DUB','English audio is required and no usable Dual/Multi Audio release was advertised, or every attempted release failed and at least one lacked a required audio track.','Waiting for a dub; no verified Japanese and English audio release is available.','New Dual/Multi Audio autobrr releases can still start Intake. Use Search again to check Prowlarr for an eligible release.','INFO',True),
    Reason('SONARR_CANDIDATES_EXHAUSTED','Every ranked release found for this episode failed or could not be acquired.','All available releases have failed; review this episode.','Check the episode on your indexers, then retry to search again.','WARNING',False),
    Reason('SHOKO_CONFIGURATION_INVALID','Optional Shoko address or API key is missing or rejected.','Configure optional Shoko metadata.','Update Shoko settings/credentials, then retry Shoko metadata. The episode remains kept.','WARNING',False),
    Reason('SHOKO_UNAVAILABLE','Optional Shoko metadata could not be read.','Optional Shoko metadata will retry.','Check Shoko availability. Keep and cleanup remain independent.','WARNING',True),
    Reason('SHOKO_METADATA_REVIEW','Shoko did not return the expected AniDB anime and episode records.','Optional Shoko metadata needs review.','Check the Shoko catalog and saved AniDB IDs, then retry Shoko metadata. The Sonarr mapping is unchanged.','WARNING',False),
    Reason('DECISION_DOWNLOAD_CLEANUP_COMPLETE','The owned torrent is absent and its recorded download files have been removed.','Download cleanup completed.','Staged/shared metadata, permanent media and episode history are retained.','INFO',False),
    Reason('DECISION_DOWNLOAD_CLEANUP_WAITING','Waiting for drained work or reconciliation of a saved torrent control request.','Download cleanup is waiting.','The worker rechecks exact ownership and may repeat safe stop or torrent-only remove commands. Local files are cleaned only after torrent absence is confirmed.','INFO',None),
    Reason('DECISION_DOWNLOAD_CLEANUP_UNAVAILABLE','The download client or filesystem is temporarily unavailable.','Download cleanup will retry.','Check the download client and storage mounts. The staged video and any Keep or Reject result remain valid.','WARNING',True),
    Reason('DECISION_DOWNLOAD_CLEANUP_REVIEW','Download ownership, source identity, staging or decision completion could not be confirmed.','Download cleanup needs review.','Review the download client and recorded files, then retry download cleanup.','WARNING',False),
    Reason('CANCELED_RESERVATIONS_RELEASED','Cancellation cleanup completed and its acquisition slots were retired without deleting history.','Canceled episode slots are available for a later intake.','A later verified release may reserve the episode again. Kept episodes remain protected.','INFO',False),
    Reason('POST_KEEP_CLEANUP_COMPLETE','Selected staged artifacts were removed after confirming the permanent result.','Selected staging files removed.','The episode remains in the permanent library.','INFO',False),
    Reason('POST_KEEP_CLEANUP_WAITING','Another attempt is still cleaning this kept episode.','Waiting to finish staged-file cleanup.','The worker will check again. The episode remains kept.','INFO',None),
    Reason('POST_KEEP_CLEANUP_UNAVAILABLE','Sonarr or the staging/permanent filesystem is temporarily unavailable.','The episode is kept, but staged-file cleanup will retry.','Check service and mount availability.','WARNING',True),
    Reason('POST_KEEP_CLEANUP_REVIEW','The saved Keep result or selected staging artifacts could not be confirmed.','The episode is kept, but staged-file cleanup needs review.','Check the permanent file and selected staged artifacts, then submit Keep again to resume only cleanup.','WARNING',False),
    Reason('JELLYFIN_CONFIGURATION_INVALID','The Jellyfin endpoint or API key is missing or rejected.','Configure Jellyfin refresh settings and credentials.','Review the address and private API key. Completed staging and Keep remain valid.','WARNING',False),
    Reason('JELLYFIN_UNAVAILABLE','Jellyfin did not accept the library scan request.','Jellyfin refresh is temporarily unavailable.','The refresh job will retry; completed media operations remain valid.','WARNING',True),
    Reason('JELLYFIN_REFRESH_REVIEW','The server returned an unexpected scan response.','Jellyfin refresh needs review.','Check the address and server API compatibility.','WARNING',False),
    Reason('KEEP_CONFIGURATION_INVALID','Sonarr endpoint, credentials, profile or library root is unconfigured.','Configure Sonarr before keeping this episode.','Review Sonarr settings and credentials, then submit Keep again.','WARNING',False),
    Reason('KEEP_IMPORT_WAITING','The saved series or import request is being reconciled.','The Keep job is in progress.','The job will check Sonarr again shortly.','INFO',None),
    Reason('KEEP_SERVICE_UNAVAILABLE','The configured Sonarr service could not be reached.','Sonarr is temporarily unavailable.','The job will retry; check the Sonarr connection if this continues.','WARNING',True),
    Reason('KEEP_IMPORT_REVIEW','The selected file, TVDB mapping or saved import result could not be confirmed.','The Keep job needs review.','Check the staged file and Sonarr import result, then submit Keep again.','WARNING',False),
    Reason('KEEP_COMPLETE','Sonarr episode-file binding and the permanent file are confirmed.','This episode has been kept.','The episode is available in the permanent library.','INFO',False),
    Reason('ANIME_REJECTED', 'An explicit Reject decision stops acquisition for this AniDB anime.', 'This anime was rejected; automatic acquisition is stopped.', 'Review the recorded Reject decision before acquiring more episodes.', 'INFO', False),
    Reason('REJECT_CLEANUP_REVIEW', 'Selected staged files could not be removed according to their recorded paths.', 'The anime is rejected, but staged-file cleanup needs attention.', 'Review the selected staged files and the existing Reject job.', 'WARNING', False),
    Reason('STAGING_REVIEW', 'Destination, copy identity, source digest or publication evidence could not be reconciled.', 'Staging needs review; existing files have been preserved.', 'Check the worker logs and recorded staging paths, then retry this staging job. Recorded partial files remain available for recovery and cleanup.', 'WARNING', False),
    Reason('STAGING_UNAVAILABLE', 'A staging filesystem operation failed transiently.', 'The staging filesystem is temporarily unavailable.', 'Check mounts, free space and worker permissions.', 'ERROR', True),
    Reason('STAGING_CONFIGURATION_INVALID', 'An accessible disjoint staging root, non-root worker or Linux no-replace publication is unavailable.', 'Configure an accessible separate staging folder and a non-root worker.', 'Configure the staging folder and worker service permissions.', 'WARNING', None),
    Reason('STAGING_BUSY', 'An exact publication attempt or shared series-metadata operation is still active.', 'Waiting for another staging operation to finish.', 'The worker will check again shortly.', 'INFO', None),
    Reason('MEDIA_VERIFICATION_INVALID', 'The selected file, container or pinned content changed or failed validation.', 'The downloaded video needs verification review.', 'Review the selected file and its recorded acquisition evidence.', 'WARNING', False),
    Reason('MEDIA_VERIFICATION_UNAVAILABLE', 'Local selected-file reads or the bounded probe timed out.', 'The downloaded video could not be checked yet.', 'Check the shared download mount and file readability.', 'ERROR', None),
    Reason('MEDIA_PROBE_UNAVAILABLE', 'The fixed local ffprobe executable could not be started.', 'The local media probe is unavailable.', 'Install ffprobe at /usr/bin/ffprobe and check worker permissions.', 'WARNING', False),
    Reason('MEDIA_AUDIO_REQUIRED', 'The non-commentary audio language tags required by the audio check setting were not established.', 'The video is missing a required audio track.', 'Review the audio streams or the Audio check setting; filenames and subtitles do not satisfy this requirement.', 'WARNING', False),
    Reason('TORRENT_METADATA_CONFIGURATION_INVALID', 'qBittorrent metadata inspection needs valid client configuration and credentials.',
           'Configure qBittorrent before inspecting torrent metadata.', 'Check the qBittorrent server, download directory and stored credentials.', 'WARNING', False),
    Reason('TORRENT_METADATA_CLIENT_UNSUPPORTED', 'The connected Web API cannot retrieve portable hash-verified torrent metadata.',
           'This qBittorrent version cannot export the batch torrent metadata.', 'The older-client batch path needs an owned, stopped magnet admission and verified inventory; do not submit or adopt an unrelated torrent.', 'WARNING', False),
    Reason('TORRENT_METADATA_UNAVAILABLE', 'A qBittorrent metadata-only request failed or timed out.',
           'qBittorrent metadata retrieval is temporarily unavailable.', 'Check the qBittorrent connection if retries continue failing.', 'WARNING', True),
    Reason('TORRENT_METADATA_WAITING', 'qBittorrent has not yet supplied an acceptable torrent file inventory.',
           'Waiting for torrent metadata.', 'The worker will retry metadata discovery for the same hash. Older qBittorrent uses the existing journal-owned magnet; newer versions can fetch metadata without adding a torrent.', 'INFO', None),
    Reason('TORRENT_METADATA_TIMEOUT', 'An admitted magnet did not provide metadata within ten minutes.',
           'The torrent metadata timed out.', 'The worker will safely retire this candidate before trying the next release.', 'WARNING', False),
    Reason('TORRENT_METADATA_DENIED', 'qBittorrent rejected the metadata-only request.',
           'qBittorrent refused the torrent metadata request.', 'Check client authentication, supported metadata operations and the torrent hash.', 'WARNING', False),
    Reason('MANAGEMENT_JOB_QUEUED', 'An existing action job is queued, so another action cannot start.', 'A job is already queued.', 'Wait for the job to start.', 'INFO', None),
    Reason('MANAGEMENT_JOB_RUNNING', 'An existing action job is running, so another action cannot start.', 'A job is already in progress.', 'Wait for the job to finish.', 'INFO', None),
    Reason('MANAGEMENT_JOB_WAITING', 'An existing action job is waiting for its next check or attempt.', 'A job is waiting to continue.', 'Check the job status for details.', 'INFO', None),
    Reason('MANAGEMENT_JOB_ATTENTION', 'An existing action job stopped and needs operator attention.', 'The job needs attention before it can continue.', 'Review the job status for details.', 'WARNING', False),
    Reason('MANAGEMENT_JOB_COMPLETE', 'The existing action job has finished.', 'This job is already complete.', 'Refresh the status to see available actions.', 'INFO', False),
    Reason('MANAGEMENT_JOB_CANCELED', 'The existing action job has been canceled.', 'This job was canceled.', 'Review the job status for details.', 'INFO', False),
    Reason('MANAGEMENT_ACTION_UNAVAILABLE', 'The target is not eligible for the explicit management action.', 'This action is not available for this work.', 'Refresh the work status and review its available actions.', 'WARNING', False),
    Reason('BULK_MANAGEMENT_PARTIAL', 'Some selected jobs did not allow the requested action; successful changes remain recorded.', 'Some bulk actions failed and the job was canceled.', 'Check the selected jobs and retry only eligible items. Successful actions were not rolled back.', 'WARNING', False),
    Reason('MANAGEMENT_JOB_SUBMITTED', 'An authorized action and its control job were committed atomically.', 'Job submitted.', 'Follow the job status for progress.', 'INFO', None),
    Reason('MANAGEMENT_JOB_EXISTS', 'A durable action already exists for this operation and action kind.', 'This action already has a job.', 'Follow the existing job instead of submitting another.', 'INFO', False),
    Reason('KEEP_NOT_READY', 'Media promotion requires the future verified staging and permanent-publication library.', 'Keeping videos is not available yet.', 'Keep is not available in this version. A submitted request will wait until Keep is supported.', 'WARNING', False),
    Reason('API_AUTH_REQUIRED', 'Request credentials are missing, invalid or conflicting.', 'Authentication is required.', 'Use the credential required for this API route.', 'WARNING', False),
    Reason('API_AUTH_UNCONFIGURED', 'Required route-specific access credentials are not configured.', 'API access has not been configured.', 'Initialize the separate management and webhook access keys locally.', 'WARNING', False),
    Reason('API_CSRF_REQUIRED', 'A browser mutation lacks the session-bound anti-forgery token.', 'This browser request could not be verified.', 'Refresh your session and try again.', 'WARNING', False),
    Reason('API_ORIGIN_INVALID', 'Request origin or host differs from the configured application origin.', 'This request came from an unexpected address.', 'Use the configured dashboard address.', 'WARNING', False),
    Reason('MANUAL_RELEASE_EPISODE_MISMATCH', 'A manually submitted release title or torrent file names a different episode than the one it was submitted for.', 'This release is for a different episode than the one selected.', 'Check the episode in the release title, or submit it from that episode.', 'WARNING', False),
    Reason('MANUAL_RELEASE_TITLE_UNREADABLE', 'No episode could be read from a manually submitted release title.', 'The release title does not say which episode it is.', 'Use the full release name, including its season and episode (for example S01E05).', 'WARNING', False),
    Reason('MANUAL_RELEASE_SOURCE_INVALID', 'A manually submitted magnet link or torrent file failed validation.', 'The magnet link or torrent file is not valid.', 'Paste a magnet link that contains its info hash, or choose a valid .torrent file.', 'WARNING', False),
    Reason('API_REQUEST_INVALID', 'Request framing, JSON structure, fields or pagination failed validation.', 'The API request is invalid.', 'Review the required request fields.', 'WARNING', False),
    Reason('API_WEBHOOK_INVALID', 'Release name or supported v1 hash is missing, ambiguous or invalid.', 'The incoming release details are incomplete or invalid.', 'Send a release name and matching 40-character torrent hash.', 'WARNING', False),
    Reason('API_BODY_TOO_LARGE', 'The request exceeds the bounded body size.', 'The request is too large.', 'Send a smaller request.', 'WARNING', False),
    Reason('API_MEDIA_TYPE_INVALID', 'Unsupported request content type or encoding.', 'This request format is not supported.', 'Send UTF-8 JSON.', 'WARNING', False),
    Reason('API_ROUTE_NOT_FOUND', 'No versioned route or requested resource exists.', 'The requested API resource was not found.', 'Check the API address and resource identifier.', 'INFO', False),
    Reason('API_METHOD_INVALID', 'The HTTP method is not supported by this route.', 'This action is not supported at this address.', 'Use the documented API method.', 'WARNING', False),
    Reason('API_RATE_LIMITED', 'A bounded authentication or session limit was reached.', 'Too many requests; try again shortly.', 'Wait before trying again.', 'WARNING', None),
    Reason('API_UNAVAILABLE', 'An API operation failed without safely exposing internal error text.', 'The API is temporarily unavailable.', 'Check service status and try again later.', 'ERROR', None),
    Reason('JOB_CANCELED', 'An operator explicitly canceled unfinished work.', 'This work was canceled.', 'Check cleanup status for any remaining downloads.', 'INFO', False),
    Reason('CANCELLATION_DRAINING', 'Cancellation is durable, but cleanup has not obtained the exclusive operation lock.', 'Waiting for the canceled operation to release its lock.', 'If the operation has no active jobs and remains blocked, restart the Anime Discovery worker to release locks from that process. If cleanup stops for review, inspect the recorded failed check and retry; reservations remain protected.', 'INFO', None),
    Reason('CANCELLATION_PENDING', 'Cleanup is incomplete; verified torrent stop and torrent-only removal can be retried after checking current ownership.', 'Cancellation cleanup is waiting for confirmation.', 'Check the existing torrent and cleanup status.', 'INFO', None),
    Reason('CANCELLATION_REVIEW', 'Cleanup ownership, file identity, sharing or external outcome cannot be established safely.', 'Cancellation cleanup needs attention.', 'The failed check and the file it looked at are shown below and in History. For the full picture (job history, ad- download folder, staging paths) run sudo adm report job --id with this job number. Fix or remove only that file, then press Retry. Never delete the whole staging or torrent root.', 'WARNING', False),
    Reason('CANCELLATION_COMPLETE', 'Owned-resource and registered temporary-file cleanup completed or no resources existed.', 'Cancellation cleanup is complete.', 'No further cleanup is required for the recorded resources.', 'INFO', None),
    Reason('TORRENT_SELECTION_UNCERTAIN', 'Fixed file selection was authorized once; confirmation is pending.', 'Video file selection is waiting for confirmation.', 'Inspect the existing selection; do not repeat uncertain commands automatically.', 'INFO', None),
    Reason('TORRENT_SELECTION_CONFIRMED', 'Exact selected-video and skipped-sidecar priorities were observed while stopped.', 'The video file is selected and ready to download.', 'Continue with the recorded download start.', 'INFO', None),
    Reason('TORRENT_PROBE_STOP_UNCERTAIN','Completed E01 requires a single stopped-torrent request whose outcome is not yet confirmed.', 'Stopping the E01 probe is awaiting confirmation.', 'Recheck qBittorrent ownership and stopped state; do not reissue an uncertain stop.', 'INFO', None),
    Reason('TORRENT_START_UNCERTAIN', 'A single durable start authorization exists; stopped or checking observations cannot rearm it.', 'Download startup is waiting for confirmation.', 'Inspect the existing download; do not restart it automatically.', 'INFO', None),
    Reason('TORRENT_DOWNLOAD_IN_PROGRESS', 'The client reports active transfer with exact selection and ownership.', 'The selected video is downloading.', 'Wait for the download to finish.', 'INFO', None),
    Reason('TORRENT_DOWNLOAD_COMPLETE', 'The client reports selected-file progress of one with matching provenance.', 'The download client reports that the selected video is complete.', 'Verify the local video before making it available.', 'INFO', None),
    Reason('TORRENT_DOWNLOAD_WAITING', 'The owned torrent is absent from a current authoritative inventory observation.', 'Waiting for the existing download to appear.', 'Check the download client; do not add another torrent automatically.', 'INFO', None),
    Reason('TORRENT_TRANSFER_CONFLICT', 'Transfer ownership, priorities, piece geometry, state or immutable evidence contradicts the journal.', 'Download details changed and need review.', 'Review ownership, selected files and the existing download before continuing.', 'WARNING', False),
    Reason('TORRENT_PIECE_OVERLAP', 'Selected video shares boundary pieces with unwanted file bytes; zero-overlap policy blocks start.', 'This video shares download pieces with files that are not selected.', 'Review the torrent layout or choose a source containing only the video.', 'WARNING', False),
    Reason('TORRENT_SNAPSHOT_CHANGED', 'Client state changed during inspection or is temporarily checking or moving.', 'The download is changing or being checked.', 'Wait for a stable status check.', 'INFO', None),
    Reason("IDENTITY_INCOMPLETE", "Required provider-verified identity facts are missing.",
           "Anime identification is incomplete.", "Review the missing identification details.",
           "WARNING", None),
    Reason("IDENTITY_INVALID", "The resolved identity failed AnimeSeries validation.",
           "Anime identification could not be verified.", "Review the identification results.",
           "WARNING", False),
    Reason("IDENTITY_CONFLICT", "Verified identifiers conflict with stored identity.",
           "Conflicting anime information needs review.", "Review the conflicting identifiers.",
           "WARNING", False),
    Reason("DOWNLOAD_CLIENT_UNAVAILABLE", "A bounded client request failed; submission outcome may require reconciliation.",
           "The download client could not be reached safely.", "Check client availability; preserve any pending submission intent.", "WARNING", None),
    Reason("DOWNLOAD_CLIENT_RESPONSE_INVALID", "Client response shape or provenance observation failed validation.",
           "The download client returned information that needs review.", "Review client compatibility and recorded source evidence.", "WARNING", False),
    Reason("DOWNLOAD_CLIENT_AUTH_FAILED", "The configured client did not accept session authentication.",
           "The download client rejected authentication.", "Review private client credentials and access settings.", "WARNING", False),
    Reason("DOWNLOAD_CLIENT_UNSUPPORTED", "qBittorrent must be 4.5+ with Web API 2.8.14+.",
           "This qBittorrent version is not supported.", "Upgrade to qBittorrent 4.5 or newer (Web API 2.8.14+) before retrying.", "WARNING", False),
    Reason("TORRENT_SUBMISSION_UNCERTAIN", "A durable one-shot intent cannot yet be confirmed; never replay its add.",
           "The torrent submission is waiting for confirmation.", "Check the existing submission; do not add it again automatically.", "INFO", None),
    Reason("TORRENT_ALREADY_PRESENT", "Exact-hash inventory existed before this application's send authorization.",
           "This torrent already exists in qBittorrent. No new torrent was submitted.", "Check the existing torrent and its originating job before retrying.", "WARNING", False),
    Reason("TORRENT_PROVENANCE_CONFLICT", "Observed marker, destination, category, management, state or files contradict the saved intent.",
           "Torrent ownership or file details need review.", "Compare the existing torrent with the saved submission intent.", "WARNING", False),
    Reason("TORRENT_STOPPED_CONFIRMED", "Saved intent matches authoritative stopped torrent provenance and file inventory.",
           "The submitted torrent is confirmed and stopped.", "Apply approved file selection before starting any download.", "INFO", None),
    Reason("DOWNLOAD_CONFIGURATION_INVALID", "Download endpoint, local folder or credentials are incomplete or invalid.",
           "Configure the download client and download folder before acquisition.", "Review download settings and private credentials.", "WARNING", False),
    Reason("ACQUISITION_SIZE_LIMIT", "The selected video is larger than the per-episode size limit.",
           "The video is larger than the episode size limit.", "Raise 'Maximum video size' in Settings, or wait for a smaller release of this episode.", "WARNING", False),
    Reason("ACQUISITION_PLAN_SIZE_LIMIT", "Adding this video would exceed the total size limit for this anime's trial episodes (selection plan).",
           "This anime's trial episodes would exceed their total size limit.", "The selection plan is every episode picked for this anime's trial. Raise 'Maximum selection-plan size' in Settings.", "WARNING", False),
    Reason("ACQUISITION_SIZE_EVIDENCE_MISSING", "A prior prepared plan lacks independently recorded selected-byte evidence.",
           "An existing acquisition plan needs its file size reviewed.", "Reinspect the existing source before adding more work to this plan.", "WARNING", False),
    Reason("ACQUISITION_SOURCE_MISSING", "A normalized v1 torrent identity is absent or unsupported.",
           "A supported torrent identity is required before acquisition.", "Check the incoming release source.", "WARNING", False),
    Reason("ACQUISITION_METADATA_INVALID", "Hash-bound metadata or its single-file declaration could not be validated.",
           "The torrent file list could not be verified.", "Review the source and declared episode before proceeding.", "WARNING", False),
    Reason("ACQUISITION_PLAN_CONFLICT", "Reservation, source or immutable preparation facts changed.",
           "Acquisition details changed and need review.", "Review the recorded episode reservation and source.", "WARNING", False),
    Reason("ACQUISITION_PLAN_READY", "Hash-bound single-video metadata and a stable submission marker were recorded.",
           "Source files have been inspected and the acquisition plan is ready.", "Complete download policy and client setup before submission.", "INFO", None),
    Reason("EPISODE_RESERVATION_GRANTED", "A selected verified episode has an exclusive durable reservation.",
           "This episode is reserved for acquisition.", "Continue with source verification before starting a download.", "INFO", None),
    Reason('EPISODE_ALREADY_STAGED','Another operation has staged this verified episode, so competing held work was canceled.','Canceled: episode already staged.','No action needed. The competing release was skipped and only its owned resources are cleaned up; the staged episode is retained.','INFO',False),
    Reason("EPISODE_RESERVED", "Another job owns the exact verified episode reservation.",
           "Another job has reserved this episode.", "Review the existing reservation before resuming this job.", "INFO", None),
    Reason("EPISODE_RESERVATION_CONFLICT", "Episode evidence, selection or reservation identity is inconsistent.",
           "Episode reservation details need review.", "Check the episode identity and recorded reservation.", "WARNING", False),
    Reason("TRIAL_SELECTED", "The verified received episode falls within its immutable episode-selection window.",
           "This episode is selected for Intake.", "Continue only after acquisition prerequisites are verified.", "INFO", None),
    Reason("TRIAL_OUTSIDE_WINDOW", "The received episode is outside the recorded episode-selection windows.",
           "This episode is outside the Intake selection.", "Review the recorded episode window if a different selection is needed.", "INFO", False),
    Reason("TRIAL_DISABLED", "Both counts are zero and no prior episode-selection plan exists.",
           "Intake episode selection is disabled.", "Set a backfill or following count to create new selection plans.", "INFO", False),
    Reason("RELEASE_AUDIO_UNTAGGED", "The audio check requires English and the autobrr release title has no Dual/Multi Audio or English dub tag.",
           "This release is not being downloaded; its episode is searched in Prowlarr instead.", "Nothing to do. Set Audio check to japanese or off to download such releases directly.", "INFO", None),
    Reason("TRIAL_PLAN_CONFLICT", "The source verification or episode-selection plan is inconsistent.",
           "The Intake selection plan needs review.", "Review the verified episode and recorded selection plan.", "WARNING", False),
    Reason("RELEASE_UNRESOLVED", "A release has missing, ambiguous or unsupported declared episode evidence.",
           "This episode could not be confirmed, so the job has stopped.", "The saved reason does not identify the failed check. Retry episode verification to record more detail.", "WARNING", False),
    Reason("RELEASE_TITLE_UNCLEAR", "The release title does not declare one supported regular episode.",
           "The release title does not clearly name one episode.", "Check the episode number in the release title. This job cannot download until one episode is identified.", "WARNING", False),
    Reason("RELEASE_TYPE_UNSUPPORTED", "The title declares a pack, range or unsupported release type.",
           "This release is a batch, range, movie, or unsupported media type.", "Check the release scope. A show's title containing 'Special' is not itself evidence of a special release.", "WARNING", False),
    Reason("RELEASE_SPECIAL_UNSUPPORTED", "No explicit Anime-Lists mapping verifies this TVDB Season 0 special from a regular AniDB episode.",
           "This Season 0 special has no verified AniDB-to-TVDB episode mapping.", "Check Anime-Lists for an explicit regular AniDB episode mapping to the TVDB Season 0 entry. Separate AniDB special numbers cannot be assumed equivalent.", "WARNING", False),
    Reason("RELEASE_MAPPING_MISSING", "No unique Anime-Lists record is available for the selected AniDB work.",
           "Anime-Lists has no single matching episode-mapping record for this anime.", "The series was identified, but its episodes cannot be matched to TVDB yet. Check the Anime-Lists entry before retrying verification.", "WARNING", False),
    Reason("RELEASE_MAPPING_UNSUPPORTED", "An Anime-Lists episode mapping rule cannot be interpreted safely.",
           "The episode-numbering rule in Anime-Lists is not supported.", "Check the Anime-Lists episode mapping. This job has stopped rather than guess which episode to download.", "WARNING", False),
    Reason("RELEASE_ANIDB_EPISODE_MISSING", "AniDB does not list the declared regular episode number.",
           "AniDB does not list this episode number for the identified anime.", "Check the release episode number and AniDB episode list. If AniDB is missing the episode, retry verification after its list is updated.", "WARNING", False),
    Reason("RELEASE_EPISODE_MAPPING_MISSING", "No mapping connects the declared episode to the other provider.",
           "The anime was found, but Anime-Lists does not connect this episode between AniDB and TVDB.", "Check the episode mapping in Anime-Lists. If it is missing, retry verification after the mapping is added. This job will not continue automatically.", "WARNING", False),
    Reason("RELEASE_EPISODE_MAPPING_AMBIGUOUS", "Multiple AniDB episodes match the declared TVDB season and episode.",
           "More than one AniDB episode matches this season and episode number.", "Check the Anime-Lists episode mapping. This job has stopped because it cannot choose one episode safely.", "WARNING", False),
    Reason("RELEASE_TVDB_EPISODE_MISSING", "TVDB does not list the mapped season and episode.",
           "TVDB does not list the season and episode supplied by the mapping.", "Compare the Anime-Lists mapping with the TVDB episode list. Retry verification after the missing entry or incorrect mapping is fixed.", "WARNING", False),
    Reason("SONARR_SELECTION_CONFLICT", "An episode's candidate releases or selection state are inconsistent.",
           "The release choice for this episode needs review.", "Review the episode's candidate releases.", "WARNING", False),
    Reason("BATCH_SELECTION_CONFLICT", "A season's candidate releases or selection state are inconsistent.",
           "The release choice for this season needs review.", "Review the season's candidate releases.", "WARNING", False),
    Reason("RELEASE_CONFLICT", "Release mapping, episode ownership or persisted source evidence conflicts.",
           "Release episode information conflicts.", "Review the episode identifiers and mapping evidence.", "WARNING", False),
    Reason("PROVIDER_UNAVAILABLE", "A provider request could not complete due to availability.",
           "An information provider is temporarily unavailable.", "The operation can be retried.",
           "WARNING", True),
    Reason("PROVIDER_CONFIGURATION_INVALID", "Required AniDB client or TVDB API-key configuration is missing or invalid.",
           "Provider configuration is incomplete or invalid.", "Configure the registered AniDB client and TVDB API key before starting identity work.", "ERROR", False),
    Reason("SECRET_STORAGE_UNAVAILABLE", "The private credential file could not be read or published safely.",
           "Credentials could not be accessed safely.", "Review credential storage, permissions and file format.", "ERROR", None),
    Reason("PROVIDER_RESPONSE_INVALID", "A provider response failed bounded decoding or structural checks.",
           "An information provider returned an unusable response.", "Retry later or review provider availability.", "WARNING", None),
    Reason("PROVIDER_AUTH_FAILED", "Provider authentication or authorization failed.",
           "An information provider could not authorize access.", "Review the provider access settings.", "WARNING", False),
    Reason("JOB_LEASE_EXPIRED", "A running job lease expired before completion.",
           "Processing was interrupted.", "Check the interrupted work and its recovery status.",
           "WARNING", True),
    Reason("JOB_HANDLER_FAILED", "A job handler failed unexpectedly.",
           "An unexpected processing error occurred.", "Review the job details.",
           "ERROR", None),
    Reason("JOB_RETRY_EXHAUSTED", "A job reached its allowed attempt limit.",
           "Automatic retries have stopped.", "Review the job before retrying manually.",
           "ERROR", False),
    Reason("STORAGE_UNAVAILABLE", "Storage access failed; the cause and commit outcome require investigation.",
           "Processing is temporarily unavailable.", "Check database health and storage.",
           "ERROR", None),
    Reason("STORAGE_STATEMENT_INVALID", "A database statement was rejected as invalid, such as a wrong number of bound values; this is a programming defect, not a storage outage. The statement did not run.",
           "An internal database statement error stopped processing.", "Report the failed check from the log; retrying unchanged code will fail the same way.",
           "ERROR", False),
    Reason("UNSUPPORTED_JOB_KIND", "No handler is registered for this job type.",
           "This type of work cannot be processed.", "Check the installed components.",
           "ERROR", False),
    Reason("TRIAL_COUNTS_INVALID", "Both episode selection counts are zero.",
           "Set backfill or episodes starting with the received episode to at least 1.",
           "Increase at least one of the two episode counts.", "WARNING", False),
    Reason("CONFIGURATION_INVALID", "Configuration shape, field or value failed validation.",
           "Configuration could not be validated.", "Review the configuration and supported settings.",
           "WARNING", False),
    Reason("CONFIGURATION_CONFLICT", "The saved revision or migration source changed.",
           "Configuration changed before this update could be applied.",
           "Refresh the configuration and review the changes again.", "WARNING", False),
    Reason("CONFIGURATION_MIGRATION_REQUIRED", "An older supported configuration needs explicit conversion.",
           "Configuration needs an upgrade.", "Preview and apply the configuration migration.",
           "WARNING", False),
    Reason("CONFIGURATION_STORAGE_FAILED", "Safe configuration access or publication failed.",
           "Configuration storage is unavailable.", "Check the service account permissions and storage.",
           "ERROR", None),
    Reason("CONFIGURATION_DURABILITY_UNCONFIRMED", "Rename succeeded but directory synchronization failed.",
           "Configuration was updated, but durable storage could not be confirmed.",
           "Read the current configuration and check storage before retrying.", "ERROR", None),
    Reason("MAINTENANCE_PENDING", "A durable maintenance request blocks new admissions.",
           "New work is paused for maintenance.", "Wait for maintenance to finish.", "INFO", None),
    Reason("MAINTENANCE_BUSY", "Existing admissions or another maintenance controller still hold a lock.",
           "Existing work is still finishing.", "Check maintenance progress before trying again.", "INFO", None),
    Reason("MAINTENANCE_FAILED", "Maintenance state, ownership or storage needs review.",
           "Maintenance state needs review.", "Check maintenance status before resuming work.", "ERROR", None),
    Reason("DATABASE_VERSION_UNSUPPORTED", "The database version is unversioned, newer or outside the supported upgrade path.",
           "This database version cannot be upgraded by this application.",
           "Verify the application version and selected database.", "ERROR", False),
    Reason("DATABASE_SCHEMA_INVALID", "Schema, integrity or foreign-key validation failed.",
           "The database does not match the expected application structure.",
           "Keep maintenance active and review database health.", "ERROR", False),
    Reason("DATABASE_MIGRATION_CONFLICT", "Database structure/version or packaged migration inputs changed after preview.",
           "The database upgrade plan changed.", "Preview the upgrade again before applying it.", "WARNING", False),
    Reason("DATABASE_MIGRATION_FAILED", "Upgrade or backup failed; earlier version steps may already be committed.",
           "Database upgrade stopped. Maintenance remains active.",
           "Review the current database version and backup before continuing.", "ERROR", None),

    Reason("JOB_WAITING", "A handler deferred a normal prerequisite or external-work wait.",
           "Work is waiting for a prerequisite.", "Check the recorded waiting condition.", "INFO", None),
    Reason("JOB_STATE_INVALID", "A queued job has inconsistent lifecycle bookkeeping.",
           "Job progress needs review.", "Review the job history before resuming work.", "ERROR", False),
    Reason("JOB_PAYLOAD_INVALID", "A queued payload is invalid or exceeds the worker bound.",
           "Job information could not be validated.", "Review the queued job before trying again.", "ERROR", False),
    Reason("WORKER_NOT_READY", "No trusted job handlers are registered for execution.",
           "The worker is not ready to process jobs.", "Install and register the required job handlers.", "ERROR", False),

)
REGISTRY = MappingProxyType({item.code: item for item in _ENTRIES})
if len(REGISTRY) != len(_ENTRIES):
    raise ValueError("duplicate reason code")

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}", re.ASCII)
_MAX_INTEGER = (1 << 63) - 1
_CONTEXT_IDENTITY = re.compile(r'(selection|recognized|verification):(INVALID|S(?:0|[1-9][0-9]{0,3})E[1-9][0-9]{0,4}#[1-9][0-9]{0,11})', re.ASCII)
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
                 context_left=None, context_right=None,
                 delete_cache=None, cache_entries_deleted=None, cached_replay=None,
                 observed_audio=None, missing_audio=None,
                 blocking_job_id=None, operation_id=None) -> dict:
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
                       ("blocking_job_id", blocking_job_id), ("operation_id", operation_id)):
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
    for key, value in (('context_left', context_left), ('context_right', context_right)):
        if value is not None:
            if type(value) is not str or not _CONTEXT_IDENTITY.fullmatch(value):
                raise ValueError('invalid persisted identity diagnostic')
            details[key] = value
    if observed_audio is not None:
        if type(observed_audio) is not str or len(observed_audio) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in observed_audio):
            raise ValueError("invalid observed audio tags")
        details["observed_audio"] = observed_audio
    if missing_audio is not None:
        if type(missing_audio) is not str or len(missing_audio) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in missing_audio):
            raise ValueError("invalid missing audio tags")
        details["missing_audio"] = missing_audio
    if cause_reason_code is not None:
        cause = get_reason(cause_reason_code)
        if cause is None:
            raise ValueError("unregistered cause reason")
        details["cause_reason_code"] = cause.code
    return details

