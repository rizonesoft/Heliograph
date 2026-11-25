/* Who is touching what, and the day's summary. Nothing runs between calls. */
#pragma once
#include "place.h"

typedef enum { SESS_UNLOADED = 0, SESS_OK, SESS_FAILED } session_state;

typedef struct silent_cache_entry {
    char project[36];
    uint64_t us;
    bool failed;
} silent_cache_entry;

typedef struct session_cache {
    session_state state;
    session_info *items;
    size_t count, cap;
    silent_cache_entry *thresholds;
    size_t thresh_count, thresh_cap;
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

/* Shared overdue + silence classifier using one captured clock_now and lazy
   session + per-project threshold caching. Returns false when liveness or threshold
   could not be read (leaving *silent_out false). */
bool watch_classify_job(store *s, const place *here, const message *m, const job *now, uint64_t clock_now,
                        session_cache *sc, bool *overdue_out, bool *silent_out);

/* Writes summary/<day>.md once a day, UTC. */
s_result watch_summary(store *s, const place *here);
