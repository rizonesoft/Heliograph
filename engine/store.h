/* Messages, and for a job one numbered chain of events. Every file is made once.
   A change to a job is the next link of its chain: it is decided from the links
   before it, and when someone else made that link first it is decided again. */
#pragma once
#include "base.h"

enum { MOST_BODY = 16384, MOST_FILE = 32768, MOST_NOTE = 1000, MOST_LINKS = 1000, MOST_NAMES = 500000 };
typedef enum { S_OK, S_NOTHING, S_REFUSED, S_DAMAGED } s_result; /* also the exit codes */
typedef enum { OPEN, TAKEN, DONE, REFUSED } job_state;

typedef struct message {
    char *file; /* owns the strings below */
    const char *id, *time, *from, *to, *for_session, *kind, *about, *urgency, *re, *subject, *body;
    uint64_t due; /* microseconds since 1970, or 0 */
    bool archived;
} message;

typedef struct store {
    const char *folder;
    char **names; /* sorted */
    size_t count, capacity;
    bool failed;
    const char *why; /* after S_REFUSED or S_DAMAGED */
    void (*pause)(void *user, const char *stage);
    void *pause_user;
} store;

/* Who acts. A lane may have several sessions after each other; when both the
   taker and the caller name theirs, they must be the same. */
typedef struct actor {
    const char *lane, *session;
} actor;

typedef struct job {
    job_state state;
    unsigned links;
    char by[72], session[72]; /* the taker, or who closed it */
    uint64_t when;            /* of the last step */
} job;

bool address_ok(const char *address, bool may_be_all); /* project/lane, or project/all */
bool session_ok(const char *session); /* empty, or 1..64 bytes unchanged by clean(..., true) */
s_result store_open(store *s, const char *folder);
void store_close(store *s);
s_result store_message(store *s, const char *id, message *out);
s_result store_message_live(store *s, const char *id, message *out);
void message_free(message *m);

/* With a key, sending the same again gives the first message and no second one. */
s_result store_post(store *s, const message *m, const char *key, text *id);
s_result store_take(store *s, const char *id, actor me);
s_result store_release(store *s, const char *id, actor me);
s_result store_end(store *s, const char *id, actor me, bool done, const char *note);
s_result store_seen(store *s, const char *id, actor me);
s_result store_seen_archived(store *s, const char *id, actor me);
s_result store_seen_alert(store *s, const char *id, const char *owner_lane);
s_result store_seen_alert_archived(store *s, const char *id, const char *owner_lane);
bool owns(const char *lane, const message *m);      /* the owner of a project on either side */
bool dest_owns(const char *lane, const message *m); /* the owner of the destination project only */
bool store_valid_seen_event(const char *ev);        /* seen.<proj>+<lane>[+<64-hex>|~alert] */
s_result store_check(store *s, text *report);       /* S_DAMAGED when anything is wrong */

bool is_job(const message *m);
bool is_for(const message *m, actor me);
s_result store_was_seen(store *s, const char *id, actor me, bool alert, bool *seen_out);
s_result store_job(store *s, const message *m, job *out);
void clean(text *out, const char *in, bool one_line);
s_result store_parse_post(char *data, size_t length, const char *id, message *out, const char **why);
bool store_apply_step(const message *m, job *now, char *data);
