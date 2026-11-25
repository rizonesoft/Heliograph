/* Archiving closed jobs to archive/<YYYY-MM>/ without locks or data loss. */
#pragma once
#include "store.h"

/* Derives "YYYY-MM" from a message id's leading epoch-microseconds timestamp. */
bool archive_month(const char *id, char out[8]);

/* Reads and validates archive/<YYYY-MM>/<id>.post. Returns S_OK (sets out->archived = true), S_NOTHING (missing), or S_DAMAGED. */
s_result archive_read_message(store *s, const char *id, message *out);

/* Replays an archived job's steps from archive/<YYYY-MM>/. Checks for step gaps and terminal state. */
s_result archive_read_job(store *s, const message *m, job *out);

/* Persists a seen mark directly to archive/<YYYY-MM>/<id>.<seen_event>. */
s_result archive_write_seen(store *s, const char *id, const char *seen_event);

/* Visits archived messages newest month first in bounded batches, skipping IDs already emitted by the live pass.
   Returns count of damaged months or messages encountered. */
size_t archive_every(store *s, char *const *emitted, size_t emitted_count,
                     void (*each)(store *s, const message *m, const job *known, void *user), void *user);

/* Validates all archived month folders and files structurally (and against live copies during interrupted moves). */
s_result archive_check(store *s, text *report);

/* Validates archive/<YYYY-MM>/<id>.post and its archived chain to DONE or REFUSED.
   Returns S_OK (valid terminal archived job, sets *links_out if non-NULL), S_NOTHING (no archive post), or S_DAMAGED. */
s_result archive_has_terminal_job(const char *folder, const char *id, unsigned *links_out);

/* True iff live event <id>.<event> (whose live .post is missing) is a clean interrupted-archive remnant
   matching a validated archived terminal job. */
bool archive_is_clean_remnant(const char *folder, const char *id, const char *event, const char *data, size_t length);

/* Archives up to limit eligible closed jobs (0 = unlimited) and sweeps verified archive orphans.
   Returns S_OK (moved or peer-moved >= 1, no damage), S_NOTHING (0 eligible, no damage), or S_DAMAGED. */
s_result archive_store(const char *folder, size_t limit, size_t *moved, text *report,
                       void (*pause)(void *user, const char *stage), void *pause_user);

/* Daily auto-archive of at most 200 eligible jobs, gated by summary/<YYYY-MM-DD>.archived and slot files. */
s_result archive_daily(const char *folder, text *report, void (*pause)(void *user, const char *stage), void *pause_user);

#ifdef HELIO_TEST_HOOKS
extern size_t archive_batch_size;
#endif
