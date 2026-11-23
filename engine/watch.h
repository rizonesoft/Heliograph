/* Who is touching what, and the day's summary. Nothing runs between calls. */
#pragma once
#include "place.h"

/* Records what this lane is touching; adds a line to said and tells the other
   lane for each one of the same project that touches the same within 4 hours. */
s_result watch_working(store *s, const place *p, const char *me, const char *paths, text *said);

bool is_overdue(const message *m, const job *now);

/* Writes summary/<day>.md once a day, UTC. */
s_result watch_summary(store *s);
