/* Where a session is: the repository's helio.conf, and who else is there.

   helio.conf has one "name: value" a line:
     project: impossible        the project, as in project/lane
     lane: apps                 the lane of a session here that names none
     repository: heliograph     optional repository identity
     silent: 120                optional minutes before a taker is silent (default 120)
     path: ui/ apps             this lane owns what is under ui/ (any number) */
#pragma once
#include "store.h"

enum { PLACE_SILENT_DEFAULT = 120, PLACE_LEGACY_REPOS = 16 };

typedef struct place {
    char *file;
    bool found;
    char project[36], lane[36], repository[36];
    uint64_t silent_minutes;
    char why[128];
} place;

typedef struct repo_time {
    char name[36];
    uint64_t when;
} repo_time;

typedef struct session_info {
    uint64_t when;
    uint64_t order;       /* one past the session's highest order when written; older records have 0 */
    uint64_t agent_order; /* the order of the write that last named the agent */
    uint64_t agent_when;  /* when the agent was last named; a record without the line counts its own time */
    uint64_t newest;      /* from place_session: the newest time of any of the session's keys; `when` stays this record's own */
    char lane[72];
    char agent[16];
    char repository[36];
    char session[72];
    repo_time legacy_repos[PLACE_LEGACY_REPOS];
    size_t legacy_repo_count;
    bool legacy;
} session_info;

s_result place_open(place *p, const char **why); /* from this folder or one above it */
void place_close(place *p);

/* Records this repository's configured silence threshold in the store when it differs
   from the stored value, and resolves the silence threshold in microseconds for a project.
   An edit to silent: in helio.conf becomes shared-store-authoritative on the next helio
   call in that repository. place_silent_us returns P_OK on live/stored/absent-default and
   P_FAILED on I/O error or corrupt stored threshold. */
p_result place_record_silent(store *s, const place *here);
p_result place_silent_us(const store *s, const place *here, const char *project, uint64_t *out_us);

/* "project/lane" for a path, from the longest owner; "" when nobody owns it. */
void place_owner(const place *p, const char *path, text *to);

bool place_parse_session(const char *data, size_t length, session_info *out);
s_result place_hello(store *s, const char *lane, const char *agent, const char *repository, const char *session);
s_result place_session(store *s, const char *lane, const char *session, session_info *out);
s_result place_session_repo(store *s, const char *lane, const char *session, const char *repository, session_info *out);
/* The session's last sign of life in one repository, or in any when repository is empty. */
uint64_t place_session_repo_when(const session_info *info, const char *repository);
p_result place_each_session(store *s, bool (*each)(void *user, const session_info *info), void *user);
p_result place_lane_sessions(store *s, const char *lane, bool (*each)(void *user, const session_info *info), void *user);
p_result place_lane_sessions_repo(store *s, const char *lane, const char *repository, bool (*each)(void *user, const session_info *info), void *user);

/* A lane of this project (and repository, when non-empty), other than me, said hello within the time. */
s_result place_busy(store *s, const char *project, const char *repository, const char *me, uint64_t within, bool *busy);

/* Root prefix length of an absolute path; sets *win_out when Windows separators apply. */
size_t place_path_root_len(const char *path, bool *win_out);
