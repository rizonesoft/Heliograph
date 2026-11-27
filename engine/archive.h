/* Archiving closed jobs to archive/<YYYY-MM>/ without locks or data loss. */
#pragma once
#include "store.h"

/* Sets the test pause called inside archive folder operations once the folder is held (NULL in production). */
void archive_pauses(void (*pause)(void *user, const char *stage), void *user);

/* Derives "YYYY-MM" from a message id's leading epoch-microseconds timestamp. */
bool archive_month(const char *id, char out[8]);

/* Reads and validates archive/<YYYY-MM>/<id>.post. Returns S_OK (sets out->archived = true), S_NOTHING (missing), or S_DAMAGED. */
s_result archive_read_message(store *s, const char *id, message *out);

/* Replays an archived job's steps from archive/<YYYY-MM>/. Checks for step gaps and terminal state. */
s_result archive_read_job(store *s, const message *m, job *out);

/* Persists a seen mark directly to archive/<YYYY-MM>/<id>.<seen_event>. */
s_result archive_write_seen(store *s, const char *id, const char *seen_event);

/* Checks archive/<YYYY-MM>/<id>.landed.<commit>. Returns S_OK (valid), S_NOTHING (missing), or S_DAMAGED. */
s_result archive_read_landed_one(const char *folder, const char *id, const char *commit);

/* Durably copies live messages/<id>.landed.<commit> into archive/<YYYY-MM>/ and removes the live copy. */
s_result archive_promote_landed(const char *folder, const char *id, const char *commit, const char **why);

/* Visits every validated archived landed commit for id in sorted order. */
s_result archive_each_landed(const char *folder, const char *id, bool (*each)(void *user, const char *commit), void *user, const char **why);

/* Visits archived messages newest month first in bounded batches, skipping IDs already emitted by the live pass.
   Stops early when each returns false. Returns count of damaged months or messages encountered. */
size_t archive_every(store *s, char *const *emitted, size_t emitted_count,
                     bool (*visible)(const message *m, void *user),
                     bool (*each)(store *s, const message *m, const job *known, const char *landed, void *user), void *user);

/* Validates all archived month folders and files structurally (and against live copies during interrupted moves). */
s_result archive_check(store *s, text *report);

/* Whether a heads-up, finding or answer leaves the working store: not yet (under 30 days since it was sent, or sent
   in the future), held (someone it is for has not seen it) or ready. A lane has seen it when any of its seen marks
   exists; a message for one session needs that session's mark; an owner's alert mark never counts. A message for
   project/all waits for every other lane of the project with a heartbeat at or after its sending (`life`, which
   the caller reads in the message's repository when it names one), and for at least one reader. It reads nothing:
   seen holds the names of the message's seen marks ("seen.<proj>+<lane>..."), and the caller has validated them. */
typedef enum { EXPIRY_NOT_YET, EXPIRY_HELD, EXPIRY_READY } expiry;
typedef struct expiry_life {
    char lane[72];
    uint64_t when;
} expiry_life;
expiry archive_expiry(const message *m, char *const *seen, size_t seen_count, const expiry_life *life, size_t life_count, uint64_t now);

/* Validates archive/<YYYY-MM>/<id>.post and its archived chain to DONE or REFUSED.
   Returns S_OK (valid terminal archived job, sets *links_out if non-NULL), S_NOTHING (no archive post), or S_DAMAGED. */
s_result archive_has_terminal_job(const char *folder, const char *id, unsigned *links_out);

/* Reads who should see a message (the heartbeats, for project/all) and decides with archive_expiry. A sessions
   listing that fails part way is S_DAMAGED and no decision. seen holds the names of its marks, already validated. */
s_result archive_expiry_of(store *s, const message *m, char *const *seen, size_t seen_count, uint64_t now, expiry *out);

/* A finished move: a terminal archived job, or an archived post of a message that is not a job (*non_job), whose
   live post the caller found gone. S_NOTHING when nothing is archived, S_DAMAGED when the archive is. */
s_result archive_has_closed(const char *folder, const char *id, unsigned *links_out, bool *non_job);

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
