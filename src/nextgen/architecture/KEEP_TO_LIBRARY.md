# Keep to the permanent library

The selected one-episode implementation is documented in [Keep through Sonarr](../KEEP_SONARR.md), with [Jellyfin refresh](../JELLYFIN_REFRESH.md) as an independent follow-up. The consolidated implementation is PR #134; the older alternative stack in #128–#130 is superseded. Tests and CI remain unrun and no lab readiness is claimed.

After staging, an explicit Keep uses the existing staged file and provider identities. AniDB identifies the anime/episode; verified TVDB series/season/episode placement selects the Sonarr series and canonical episode. Sonarr manages permanent file placement. Shoko remains optional and cannot block or override this mapping.

The worker records series-add/import intent before writes, selects only the approved staged file and requests ManualImport in copy mode. Lost replies reconcile saved requests instead of blindly posting again. Existing series settings stay unchanged. The job records Kept only after command completion, correct episode-file binding and a readable regular permanent file at the expected size. There is no destination checksum or byte-for-byte readback. A separate cleanup job rechecks the permanent file and removes only the selected staged video and episode NFO; shared metadata remains.

Jellyfin refresh failures do not undo Keep. Refresh acceptance is not proof of indexing. Torrent/download cleanup, canceled-work reservation release and optional Shoko remain follow-up work. No fallback filesystem promotion, automatic missing-episode search or second staging pipeline is introduced.

Failures use the central readable reason catalog, including KEEP_CONFIGURATION_INVALID, KEEP_IMPORT_WAITING, SERVICE_UNAVAILABLE and KEEP_IMPORT_REVIEW. UI copy must not substitute raw exceptions or provider payloads for those explanations.

The later [download cleanup](../DECISION_DOWNLOAD_CLEANUP.md) draft adds owned
torrent/download cleanup after completed Keep/Reject source cleanup. Canceled
reservation release is also implemented in the draft stack; these remain untested.
