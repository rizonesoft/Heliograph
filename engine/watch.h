/* Who is touching what, and the day's summary. Nothing runs between calls. */
#pragma once
#include "place.h"

typedef enum { SESS_UNLOADED = 0, SESS_OK, SESS_FAILED } session_state;

typedef struct session_cache {
    session_state state;
    session_info *items;
    size_t count, cap;
} session_cache;

void sessions_cache_load(store *s, session_cache *c);
void sessions_cache_free(session_cache *c);

/* Records what this lane is touching; adds a line to said and tells the other
   lane for each one of the same project that touches the same within 4 hours. */
s_result watch_working(store *s, const place *p, const char *me, const char *paths, text *said);

bool is_overdue(const message *m, const job *now);

/* Pure function of the loaded sessions, the job and the clock: true when a TAKEN
   job's taker session (or lane, if no session was named) has shown no sign of life
   for at least silent_us microseconds. */
bool is_silent(const session_info *sessions, size_t session_count, const job *now, uint64_t clock_now, uint64_t silent_us);

/* Writes summary/<day>.md once a day, UTC. */
s_result watch_summary(store *s, const place *here);
