#include "store.h"

static s_result say(store *s, s_result result, const char *why) {
    s->why = why;
    return result;
}

static bool word(const char *s, size_t length) {
    if (!length || length > 32) return false;
    for (size_t i = 0; i < length; i++)
        if (!((s[i] >= 'a' && s[i] <= 'z') || (s[i] >= '0' && s[i] <= '9') || (s[i] == '-' && i))) return false;
    return true;
}

bool address_ok(const char *a, bool may_be_all) {
    size_t slash = 0;
    while (a[slash] && a[slash] != '/') slash++;
    if (!a[slash] || !word(a, slash) || !word(a + slash + 1, len(a + slash + 1))) return false;
    return may_be_all || !same(a + slash + 1, "all");
}

static bool id_ok(const char *id) {
    size_t n = len(id);
    if (n < 8 || n > 40) return false;
    for (size_t i = 0; i < n; i++)
        if (!((id[i] >= '0' && id[i] <= '9') || (id[i] >= 'a' && id[i] <= 'f') || (id[i] == '-' && i))) return false;
    return true;
}

static void path(const store *s, text *out, const char *id, const char *event) {
    add(out, s->folder), add(out, "/messages/"), add(out, id), add(out, "."), add(out, event);
}

static bool gather(void *user, const char *name) {
    store *s = user;
    size_t n = len(name);
    if (n > 5 && same(name + n - 5, ".part")) return true; /* being written, or left by a crash */
    if (s->count >= MOST_NAMES) {
        s->failed = true;
        return false;
    }
    if (s->count == s->capacity) {
        size_t capacity = s->capacity ? s->capacity * 2 : 256;
        char **names = p_alloc(capacity * sizeof *names);
        if (!names) {
            s->failed = true;
            return false;
        }
        if (s->count) memcpy(names, s->names, s->count * sizeof *names);
        p_free(s->names);
        s->names = names, s->capacity = capacity;
    }
    if (!(s->names[s->count] = copy(name, n))) {
        s->failed = true;
        return false;
    }
    s->count++;
    return true;
}

static void sift(char **a, size_t i, size_t n) {
    for (size_t child; (child = 2 * i + 1) < n; i = child) {
        if (child + 1 < n && compare(a[child], a[child + 1]) < 0) child++;
        if (compare(a[i], a[child]) >= 0) return;
        char *swap = a[i];
        a[i] = a[child], a[child] = swap;
    }
}

s_result store_open(store *s, const char *folder) {
    *s = (store){.folder = folder};
    text at = {0};
    add(&at, folder), add(&at, "/messages");
    if (at.failed) return say(s, S_DAMAGED, "out of memory");
    p_result result = p_list(at.data, gather, s);
    text_free(&at);
    if (result == P_MISSING) return say(s, S_DAMAGED, "there is no store here: run helio init");
    if (result != P_OK || s->failed) return say(s, S_DAMAGED, s->count >= MOST_NAMES ? "the store is too full: archive it" : "the store cannot be read");
    for (size_t i = s->count / 2; i-- > 0;) sift(s->names, i, s->count);
    for (size_t n = s->count; n > 1;) {
        char *swap = s->names[0];
        s->names[0] = s->names[--n], s->names[n] = swap;
        sift(s->names, 0, n);
    }
    return S_OK;
}

void store_close(store *s) {
    for (size_t i = 0; i < s->count; i++) p_free(s->names[i]);
    p_free(s->names);
    s->names = NULL, s->count = s->capacity = 0;
}

void message_free(message *m) {
    p_free(m->file);
    *m = (message){0};
}

/* The value of a "name: value" line, cut off in place; "" when it is not there. */
static const char *field(char *head, const char *name) {
    for (char *line = head; *line;) {
        char *end = line;
        while (*end && *end != '\n') end++;
        if (starts(line, name) && line[len(name)] == ':' && line[len(name) + 1] == ' ') {
            char *value = line + len(name) + 2;
            return value <= end ? value : "";
        }
        line = *end ? end + 1 : end;
    }
    return "";
}

static void cut(const char *value) {
    char *at = (char *)value;
    while (*at && *at != '\n') at++;
    if (*at) *at = 0;
}

s_result store_message(store *s, const char *id, message *out) {
    *out = (message){0};
    if (!id_ok(id)) return say(s, S_REFUSED, "that is not a message id");
    text at = {0};
    path(s, &at, id, "post");
    char *data = NULL;
    size_t length = 0;
    p_result result = at.failed ? P_FAILED : p_read(at.data, MOST_FILE, &data, &length);
    text_free(&at);
    if (result == P_MISSING) return say(s, S_REFUSED, "there is no such message");
    if (result != P_OK || len(data) != length) {
        if (result == P_OK) p_free(data);
        return say(s, S_DAMAGED, "the message cannot be read");
    }
    size_t split = 0;
    while (data[split] && !(data[split] == '\n' && data[split + 1] == '\n')) split++;
    if (!data[split]) {
        p_free(data);
        return say(s, S_DAMAGED, "the message has no body");
    }
    data[split + 1] = 0;
    *out = (message){data, field(data, "id"), field(data, "time"), field(data, "from"), field(data, "to"), field(data, "kind"),
                     field(data, "about"), field(data, "urgency"), field(data, "re"), field(data, "subject"), data + split + 2};
    const char *all[] = {out->id, out->time, out->from, out->to, out->kind, out->about, out->urgency, out->re, out->subject};
    for (size_t i = 0; i < sizeof all / sizeof *all; i++) if (*all[i]) cut(all[i]);
    if (!same(out->id, id) || !address_ok(out->from, false) || !address_ok(out->to, true) || !*out->subject) {
        message_free(out);
        return say(s, S_DAMAGED, "the message is not what its name says");
    }
    return S_OK;
}

bool is_job(const message *m) { return same(m->kind, "request") || same(m->kind, "handoff"); }

bool is_for(const message *m, const char *me) {
    size_t n = len(m->to);
    if (same(m->to, me)) return true;
    if (n <= 4 || !same(m->to + n - 4, "/all") || same(m->from, me)) return false;
    for (size_t i = 0; i < n - 3; i++) /* the project and its slash */
        if (me[i] != m->to[i]) return false;
    return true;
}

static bool has(const store *s, const char *id, const char *event) {
    size_t n = len(id), low = 0, high = s->count;
    while (low < high) {
        size_t mid = low + (high - low) / 2;
        const char *name = s->names[mid];
        int order = 0;
        size_t i = 0;
        for (; i < n && !order; i++) order = (unsigned char)name[i] - (unsigned char)id[i];
        if (!order) order = (unsigned char)name[n] - '.';
        if (!order) order = compare(name + n + 1, event);
        if (!order) return true;
        if (order < 0) low = mid + 1;
        else high = mid;
    }
    return false;
}

static void seen_name(text *out, const char *me) {
    add(out, "seen.");
    for (; *me; me++) add_bytes(out, *me == '/' ? "+" : me, 1);
}

bool was_seen(const store *s, const char *id, const char *me) {
    text event = {0};
    seen_name(&event, me);
    bool found = !event.failed && has(s, id, event.data);
    text_free(&event);
    return found;
}

static s_result make(store *s, const char *id, const char *event, const char *data) {
    text at = {0}, unique = {0};
    path(s, &at, id, event);
    add_number(&unique, p_tick(), 0);
    p_result result = at.failed || unique.failed ? P_FAILED : p_write(at.data, data, len(data), false, unique.data);
    text_free(&at);
    text_free(&unique);
    if (result == P_EXISTS) return say(s, S_NOTHING, "it is already there");
    return result == P_OK ? S_OK : say(s, S_DAMAGED, "the store cannot be written");
}

static void numbered(text *out, const char *event, unsigned n) {
    add(out, event), add(out, "."), add_number(out, n, 4);
}

/* Read from the disk and not from the listing, because others write meanwhile. */
static s_result event_of(store *s, const char *id, const char *event, char **data) {
    text at = {0};
    path(s, &at, id, event);
    size_t length;
    p_result result = at.failed ? P_FAILED : p_read(at.data, MOST_FILE, data, &length);
    text_free(&at);
    return result == P_OK ? S_OK : result == P_MISSING ? S_NOTHING : say(s, S_DAMAGED, "an event cannot be read");
}

/* The turn a job is in: one more than the times it was released. */
static s_result turn(store *s, const char *id, unsigned *n, char **taken) {
    *taken = NULL;
    for (*n = 0; *n < 1000; ++*n) {
        text name = {0};
        numbered(&name, "release", *n);
        char *data = NULL;
        s_result result = name.failed ? S_DAMAGED : event_of(s, id, name.data, &data);
        text_free(&name);
        if (result == S_OK) {
            p_free(data);
            continue;
        }
        if (result != S_NOTHING) return result;
        numbered(&name, "take", *n);
        result = name.failed ? S_DAMAGED : event_of(s, id, name.data, taken);
        text_free(&name);
        return result == S_NOTHING ? S_OK : result;
    }
    return say(s, S_DAMAGED, "the job was released too often");
}

s_result store_state(store *s, const char *id, job_state *state, char **taker) {
    char *data = NULL;
    *taker = NULL;
    s_result result = event_of(s, id, "end", &data);
    if (result == S_OK) *state = same(field(data, "state"), "done") || starts(field(data, "state"), "done\n") ? DONE : REFUSED;
    else if (result != S_NOTHING) return result;
    else {
        unsigned n;
        if ((result = turn(s, id, &n, &data)) != S_OK) return result;
        *state = data ? TAKEN : OPEN;
        if (!data) return S_OK;
    }
    const char *by = field(data, "by");
    cut(by);
    *taker = copy(by, len(by));
    p_free(data);
    return *taker ? S_OK : say(s, S_DAMAGED, "out of memory");
}

static bool secret(const char *t) {
    static const char *const marks[] = {"-----BEGIN ", "ghp_", "gho_", "ghs_", "ghr_", "ghu_", "github_pat_", "sk-", "ops_", "AKIA", "xoxb-", "xoxp-"};
    static const int tail[] = {12, 30, 30, 30, 30, 30, 30, 24, 30, 16, 20, 20};
    for (const char *at = t; *at; at++)
        for (size_t i = 0; i < sizeof marks / sizeof *marks; i++) {
            if (!starts(at, marks[i])) continue;
            if (!i) {
                const char *p = at + len(marks[0]);
                while (*p && *p != '\n' && !starts(p, "PRIVATE KEY")) p++;
                if (starts(p, "PRIVATE KEY")) return true;
                continue;
            }
            int n = 0;
            for (const char *p = at + len(marks[i]); (*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9') || *p == '_' || *p == '-'; p++) n++;
            if (n >= tail[i]) return true;
        }
    return false;
}

/* Without control characters or marks that turn text around, so a message
   cannot drive a terminal. A line stays one line when one_line is set. */
static void clean(text *out, const char *in, bool one_line) {
    for (const unsigned char *at = (const unsigned char *)in; *at;) {
        if (*at == '\n' || *at == '\t') {
            add(out, one_line ? " " : *at == '\n' ? "\n" : "\t");
            at++;
        } else if (*at < 0x20 || *at == 0x7F) {
            at++;
        } else if (at[0] == 0xC2 && at[1] >= 0x80 && at[1] <= 0x9F) {
            at += 2;
        } else if (at[0] == 0xE2 && ((at[1] == 0x80 && ((at[2] >= 0xAA && at[2] <= 0xAE) || at[2] == 0xA8 || at[2] == 0xA9)) || (at[1] == 0x81 && at[2] >= 0xA6 && at[2] <= 0xA9))) {
            at += 3;
        } else {
            add_bytes(out, (const char *)at++, 1);
        }
    }
}

static bool one_of(const char *value, const char *const *list) {
    for (; *list; list++) if (same(value, *list)) return true;
    return false;
}

s_result store_post(store *s, const message *m, text *id) {
    static const char *const kinds[] = {"request", "handoff", "answer", "finding", "headsup", NULL}, *const urgencies[] = {"now", "next", "whenever", NULL};
    if (!address_ok(m->from, false)) return say(s, S_REFUSED, "say who you are: --as project/lane");
    if (!address_ok(m->to, true)) return say(s, S_REFUSED, "the reader is project/lane, or project/all");
    if (!one_of(m->kind, kinds)) return say(s, S_REFUSED, "the kind is request, handoff, answer, finding or headsup");
    if (!one_of(m->urgency, urgencies)) return say(s, S_REFUSED, "the urgency is now, next or whenever");
    if (len(m->subject) > 120 || len(m->about) > 1000 || len(m->body) > MOST_BODY) return say(s, S_REFUSED, "the message is too long");
    if (secret(m->subject) || secret(m->body) || secret(m->about)) return say(s, S_REFUSED, "this looks like it contains a secret, so it was not sent");
    if (*m->re) {
        message other;
        s_result result = store_message(s, m->re, &other);
        message_free(&other);
        if (result != S_OK) return result;
    }
    text subject = {0}, about = {0}, body = {0}, out = {0};
    clean(&subject, m->subject, true), clean(&about, m->about, true), clean(&body, m->body, false);
    s_result result = S_REFUSED;
    bool blank = true;
    for (const char *p = subject.data ? subject.data : ""; *p; p++) blank = blank && *p == ' ';
    if (blank || !body.data || !body.length) {
        s->why = "a message needs a subject and a body";
    } else {
        uint64_t now = p_now(), mixed = p_tick();
        add_number(id, now, 16), add(id, "-");
        for (int shift = 20; shift >= 0; shift -= 4) add_bytes(id, &"0123456789abcdef"[mixed >> (shift + 24) & 15], 1);
        add(&out, "id: "), add(&out, id->data ? id->data : ""), add(&out, "\ntime: "), add_time(&out, now);
        add(&out, "\nfrom: "), add(&out, m->from), add(&out, "\nto: "), add(&out, m->to), add(&out, "\nkind: "), add(&out, m->kind);
        add(&out, "\nabout: "), add(&out, about.data ? about.data : ""), add(&out, "\nurgency: "), add(&out, m->urgency);
        add(&out, "\nre: "), add(&out, m->re), add(&out, "\nsubject: "), add(&out, subject.data), add(&out, "\n\n"), add(&out, body.data);
        result = out.failed || id->failed || body.failed ? say(s, S_DAMAGED, "out of memory") : make(s, id->data, "post", out.data);
        if (result == S_NOTHING) result = say(s, S_DAMAGED, "two messages got one id: send it again");
    }
    text_free(&subject), text_free(&about), text_free(&body), text_free(&out);
    return result;
}

static s_result stamped(store *s, const char *id, const char *event, const char *me, const char *state, const char *note) {
    text out = {0}, line = {0};
    clean(&line, note, true);
    add(&out, "by: "), add(&out, me), add(&out, "\ntime: "), add_time(&out, p_now()), add(&out, "\nstate: "), add(&out, state);
    add(&out, "\nnote: "), add(&out, line.data ? line.data : ""), add(&out, "\n");
    s_result result = out.failed || line.failed ? say(s, S_DAMAGED, "out of memory") : make(s, id, event, out.data);
    text_free(&out), text_free(&line);
    return result;
}

/* A job that this lane may act on. */
static s_result job(store *s, const char *id, const char *me, message *m) {
    if (!address_ok(me, false)) return say(s, S_REFUSED, "say who you are: --as project/lane");
    s_result result = store_message(s, id, m);
    if (result == S_OK && !is_job(m)) {
        message_free(m);
        result = say(s, S_REFUSED, "only a request or a handoff can be taken");
    }
    return result;
}

s_result store_take(store *s, const char *id, const char *me) {
    message m;
    s_result result = job(s, id, me, &m);
    if (result != S_OK) return result;
    job_state state;
    char *taker = NULL;
    unsigned n;
    char *taken = NULL;
    if (!is_for(&m, me)) result = say(s, S_REFUSED, "it is not for you");
    else if ((result = store_state(s, id, &state, &taker)) != S_OK) {}
    else if (state != OPEN) result = say(s, S_REFUSED, state == TAKEN ? "it is already taken" : "it is already closed");
    else if ((result = turn(s, id, &n, &taken)) == S_OK) {
        if (taken) {
            result = say(s, S_REFUSED, "it is already taken");
        } else {
            text event = {0};
            numbered(&event, "take", n);
            result = event.failed ? S_DAMAGED : stamped(s, id, event.data, me, "taken", "");
            if (result == S_NOTHING) result = say(s, S_REFUSED, "it is already taken");
            text_free(&event);
        }
    }
    p_free(taker), p_free(taken), message_free(&m);
    return result;
}

static bool is_owner(const char *me) {
    size_t n = len(me);
    return n > 6 && same(me + n - 6, "/owner");
}

s_result store_release(store *s, const char *id, const char *me) {
    message m;
    s_result result = job(s, id, me, &m);
    if (result != S_OK) return result;
    job_state state;
    char *taker = NULL, *taken = NULL;
    unsigned n;
    if ((result = store_state(s, id, &state, &taker)) == S_OK) {
        if (state != TAKEN) result = say(s, S_REFUSED, "it is not taken");
        else if (!same(taker, me) && !is_owner(me)) result = say(s, S_REFUSED, "someone else took it");
        else if ((result = turn(s, id, &n, &taken)) == S_OK) {
            text event = {0};
            numbered(&event, "release", n);
            result = event.failed ? S_DAMAGED : stamped(s, id, event.data, me, "released", "");
            if (result == S_NOTHING) result = say(s, S_REFUSED, "it was released meanwhile");
            text_free(&event);
        }
    }
    p_free(taker), p_free(taken), message_free(&m);
    return result;
}

s_result store_end(store *s, const char *id, const char *me, bool done, const char *note) {
    message m;
    s_result result = job(s, id, me, &m);
    if (result != S_OK) return result;
    job_state state;
    char *taker = NULL;
    if ((result = store_state(s, id, &state, &taker)) == S_OK) {
        bool mine = state == TAKEN && same(taker, me);
        if (state == DONE || state == REFUSED) result = say(s, S_REFUSED, "it is already closed");
        else if (done && !mine) result = say(s, S_REFUSED, state == TAKEN ? "someone else took it" : "take it first");
        else if (!done && !mine && !same(m.from, me) && !is_owner(me) && !(state == OPEN && is_for(&m, me))) result = say(s, S_REFUSED, "it is not yours to refuse");
        else {
            result = stamped(s, id, "end", me, done ? "done" : "refused", note);
            if (result == S_NOTHING) result = say(s, S_REFUSED, "it was closed meanwhile");
        }
    }
    p_free(taker), message_free(&m);
    return result;
}

s_result store_seen(store *s, const char *id, const char *me) {
    if (!address_ok(me, false)) return say(s, S_REFUSED, "say who you are: --as project/lane");
    text event = {0};
    seen_name(&event, me);
    s_result result = event.failed ? S_DAMAGED : stamped(s, id, event.data, me, "seen", "");
    text_free(&event);
    return result == S_NOTHING ? S_OK : result;
}
