/* Messages and their events. Every event is one file that is made once. */
#pragma once
#include "base.h"

enum { MOST_BODY = 16384, MOST_FILE = 32768, MOST_NAMES = 20000 };
typedef enum { S_OK, S_NOTHING, S_REFUSED, S_DAMAGED } s_result; /* also the exit codes */
typedef enum { OPEN, TAKEN, DONE, REFUSED } job_state;

typedef struct message {
    char *file; /* owns the strings below */
    const char *id, *time, *from, *to, *kind, *about, *urgency, *re, *subject, *body;
} message;

typedef struct store {
    const char *folder;
    char **names; /* sorted */
    size_t count, capacity;
    bool failed;
    const char *why; /* after S_REFUSED or S_DAMAGED */
} store;

bool address_ok(const char *address, bool may_be_all); /* project/lane, or project/all */
s_result store_open(store *s, const char *folder);
void store_close(store *s);
s_result store_message(store *s, const char *id, message *out);
void message_free(message *m);

s_result store_post(store *s, const message *m, text *id);
s_result store_take(store *s, const char *id, const char *me);
s_result store_release(store *s, const char *id, const char *me);
s_result store_end(store *s, const char *id, const char *me, bool done, const char *note);
s_result store_seen(store *s, const char *id, const char *me);

bool is_job(const message *m);
bool is_for(const message *m, const char *me);
bool was_seen(const store *s, const char *id, const char *me);
/* taker gets new memory when the state is TAKEN, DONE or REFUSED. */
s_result store_state(store *s, const char *id, job_state *state, char **taker);
