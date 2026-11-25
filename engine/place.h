/* Where a session is: the repository's helio.conf, and who else is there.

   helio.conf has one "name: value" a line:
     project: impossible        the project, as in project/lane
     lane: apps                 the lane of a session here that names none
     repository: heliograph     optional repository identity
     silent: 120                optional minutes before a taker is silent (default 120)
     path: ui/ apps             this lane owns what is under ui/ (any number) */
#pragma once
#include "store.h"

enum { PLACE_SILENT_DEFAULT = 120 };

typedef struct place {
    char *file;
    bool found;
    char project[36], lane[36], repository[36];
    uint64_t silent_minutes;
    char why[128];
} place;

typedef struct session_info {
    uint64_t when;
    char lane[72];
    char agent[16];
    char repository[36];
    char session[72];
    bool legacy;
} session_info;

s_result place_open(place *p, const char **why); /* from this folder or one above it */
void place_close(place *p);

/* Records this repository's configured silence threshold in the store (best-effort)
   and resolves the silence threshold in microseconds for a given project. */
void place_record_silent(store *s, const place *here);
uint64_t place_silent_us(const store *s, const place *here, const char *project);

/* "project/lane" for a path, from the longest owner; "" when nobody owns it. */
void place_owner(const place *p, const char *path, text *to);

bool place_parse_session(const char *data, size_t length, session_info *out);
s_result place_hello(store *s, const char *lane, const char *agent, const char *repository, const char *session);
s_result place_session(store *s, const char *lane, const char *session, session_info *out);
p_result place_each_session(store *s, bool (*each)(void *user, const session_info *info), void *user);

/* A lane of this project, other than me, said hello within the time. */
s_result place_busy(store *s, const char *project, const char *me, uint64_t within, bool *busy);
