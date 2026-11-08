# Magnet readmission after cleanup

A new webhook may acquire the same torrent hash after the previous acquisition
has been canceled, fully cleaned up and released. A worker restart or an absent
torrent alone does not permit another submission.

Schema v47 keeps historical `magnet_intents` rows as `RELEASED` and applies
the unique hash claim only to unreleased rows. The original event, job, marker
and inventory remain available. Install with the existing explicit database
upgrade flow; no startup migration or database reset is needed.

Reservation release retires the canceled operation's magnet claims in its
existing transaction. Admission also handles older completed cleanups: it can
retire a historical claim only if the old operation is `CANCELED/COMPLETE`, has
no active single/batch reservation, no prepared acquisition, and no published
staging record. Otherwise a same-hash conflict becomes an acquisition review,
without inserting a duplicate row or exiting the worker.

Offline coverage includes oversized webhook cleanup followed by a new delivery
of the same hash, retaining both journals and issuing one add per admission;
active/unresolved claims remain protected. The preserved lab job still needs
a retry after the application and v47 database upgrade.
