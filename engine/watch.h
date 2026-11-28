/* Who is touching what, and the day's summary. Nothing runs between calls. */
#pragma once
#include "place.h"

typedef struct silent_cache_entry {
    char project[36];
    uint64_t us;
    bool failed;
} silent_cache_entry;

typedef struct session_query_entry {
    char lane[72];
    char session[72];    /* "" = all sessions of lane */
    char repository[36]; /* "" = canonical session, non-empty = per-repo heartbeat */
    bool failed;
} session_query_entry;

typedef struct session_cache {
    session_info *items;
    size_t count, cap;
    session_query_entry *queries;
    size_t query_count, query_cap;
    silent_cache_entry *thresholds;
    size_t thresh_count, thresh_cap;
} session_cache;

void sessions_cache_free(session_cache *c);

/* Records what this lane is touching; adds a line to said and tells the other
   lane for each one of the same project that touches the same within 4 hours. */
/* What a declaration reads back as for --json: the stored paths as a JSON array of normalized paths, and one
   {"lane", "path"} entry for each other lane of the project working on an overlapping path (the more specific one
   for a directory match). Filled once the declaration is stored, also when the notification or the read of a peer
   then fails; stored says so. */
typedef struct working_out {
    text paths, overlaps;
    bool stored;
} working_out;

s_result watch_working(store *s, const place *p, const char *me, const char *paths, text *said, working_out *out);

bool is_overdue(const message *m, const job *now);

/* Pure function of the loaded sessions, the job and the clock: true when a TAKEN
   job's taker session (or lane, if no session was named) has shown no sign of life
   for at least silent_us microseconds. */
bool is_silent(const session_info *sessions, size_t session_count, const job *now, uint64_t clock_now, uint64_t silent_us);

/* Pure function of the loaded sessions, a targeted lane+session, a reference time
   (send time or release time), and the clock: true when that session has shown no
   sign of life since since_us for at least silent_us microseconds. */
bool is_session_silent(const session_info *sessions, size_t session_count, const char *lane, const char *session,
                       uint64_t since_us, uint64_t clock_now, uint64_t silent_us);

/* Shared overdue + silence classifier using one captured clock_now and lazy
   session + per-project threshold caching. Returns false when liveness or threshold
   could not be read (leaving *silent_out false). */
bool watch_classify_job(store *s, const place *here, const message *m, const job *now, uint64_t clock_now,
                        session_cache *sc, bool *overdue_out, bool *silent_out);

/* Targeted-session silence classifier using the destination project's threshold.
   Returns false when liveness or threshold could not be read (leaving *silent_out false). */
bool watch_classify_session_silent(store *s, const place *here, const message *m, uint64_t since_us, uint64_t clock_now,
                                   session_cache *sc, bool *silent_out);

/* The largest snapshot, for the writer and the reader alike: every held id of a full store (MOST_NAMES names at up to 32
   bytes a line) and a megabyte for the parts that list their first 100 rows. */
enum { MOST_SUMMARY = MOST_NAMES * 32 + (1 << 20), SUMMARY_PARTS = 8 };

/* Writes summary/<day>.md once a day, UTC, when it is not there: one clock instant names the file, heads it and
   classifies every row. day is the file's day (YYYY-MM-DD), whatever the result. S_OK when the file is there (made
   now or by a call that was faster), S_NOTHING when a read the summary needs failed (the day is deferred and no file
   is kept), S_DAMAGED when it cannot be written. Counts the pairs of lanes of one project whose live `working`
   declarations share a path; posts no notice. */
s_result watch_summary(store *s, const place *here, char day[11]);

/* What a snapshot file says, read once the whole file is checked. format is 2 for a file with a Format line and 1 for
   one of the earlier writer, which has no overlaps part (counts[7] is 0 and format says so). */
typedef struct summary_read {
    char day[11], at[21];
    unsigned format, counts[SUMMARY_PARTS], damaged;
} summary_read;

/* True when data (length bytes) is a whole snapshot of either format: its header, every part with its count, and for
   format 2 the damage line and the end line. False for a file that is cut short or not a snapshot. */
bool watch_summary_read(const char *data, size_t length, summary_read *out);
