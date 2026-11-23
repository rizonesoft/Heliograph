/* Where a session is: the repository's helio.conf, and who else is there.

   helio.conf has one "name: value" a line:
     project: impossible        the project, as in project/lane
     lane: apps                 the lane of a session here that names none
     path: ui/ apps             this lane owns what is under ui/ (any number) */
#pragma once
#include "store.h"

typedef struct place {
    char *file;
    bool found;
    char project[36], lane[36];
} place;

s_result place_open(place *p, const char **why); /* from this folder or one above it */
void place_close(place *p);

/* "project/lane" for a path, from the longest owner; "" when nobody owns it. */
void place_owner(const place *p, const char *path, text *to);

s_result place_hello(store *s, const char *lane);
/* A lane of this project, other than me, said hello within the time. */
bool place_busy(store *s, const char *project, const char *me, uint64_t within);
