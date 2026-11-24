/* Archiving closed jobs to archive/<YYYY-MM>/ without locks or data loss. */
#pragma once
#include "store.h"

/* Derives "YYYY-MM" from a message id's leading epoch-microseconds timestamp. */
bool archive_month(const char *id, char out[8]);

/* Reads and validates archive/<YYYY-MM>/<id>.post. Returns S_OK, S_NOTHING (missing), or S_DAMAGED. */
s_result archive_read_message(store *s, const char *id, message *out);

/* True iff archive/<YYYY-MM>/<id>.post and its archived chain validate to DONE or REFUSED. */
bool archive_has_terminal_job(const char *folder, const char *id);

/* Archives up to limit eligible closed jobs (0 = unlimited) and sweeps verified archive orphans.
   Returns S_OK (moved or peer-moved >= 1, no damage), S_NOTHING (0 eligible, no damage), or S_DAMAGED. */
s_result archive_store(const char *folder, size_t limit, size_t *moved, text *report,
                       void (*pause)(void *user, const char *stage), void *pause_user);

/* Daily auto-archive of at most 200 eligible jobs, gated by summary/<YYYY-MM-DD>.archived. */
s_result archive_daily(const char *folder, void (*pause)(void *user, const char *stage), void *pause_user);
