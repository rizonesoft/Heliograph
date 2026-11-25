#include "archive.h"
#include "store.h"

#ifdef HELIO_TEST_HOOKS
size_t store_max_names = MOST_NAMES;
#else
#define store_max_names MOST_NAMES
#endif

typedef enum { TAKE, RELEASE, FINISH, REFUSE } change;

static const char *nz(const char *s) { return s ? s : ""; }

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

bool session_ok(const char *s) {
    if (!s || !*s) return true;
    size_t n = len(s);
    if (n > 64) return false;
    text cleaned = {0};
    clean(&cleaned, s, true);
    bool good = !cleaned.failed && cleaned.length == n && same(cleaned.data ? cleaned.data : "", s);
    text_free(&cleaned);
    return good;
}

static bool id_ok(const char *id) {
    size_t n = len(id);
    if (n < 8 || n > 40) return false;
    for (size_t i = 0; i < n; i++)
        if (!((id[i] >= '0' && id[i] <= '9') || (id[i] >= 'a' && id[i] <= 'f') || (id[i] == '-' && i))) return false;
    return true;
}

static bool time_ok(const char *t) {
    uint64_t when;
    return read_time(t, &when);
}

static void path(const store *s, text *out, const char *id, const char *event) {
    add(out, s->folder), add(out, "/messages/"), add(out, id), add(out, "."), add(out, event);
}

static bool gather(void *user, const char *name) {
    store *s = user;
    size_t n = len(name);
    if (n > 5 && same(name + n - 5, ".part")) return true; /* being written, or left by a crash */
    if (s->count >= store_max_names) {
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
    if (result != P_OK || s->failed) return say(s, S_DAMAGED, s->count >= store_max_names ? "the store is too full: archive it" : "the store cannot be read");
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

static const char *const kinds[] = {"request", "handoff", "answer", "finding", "headsup", NULL};
static const char *const urgencies[] = {"now", "next", "whenever", NULL};

static bool one_of(const char *value, const char *const *list) {
    for (; *list; list++) if (same(value, *list)) return true;
    return false;
}

s_result store_parse_post(char *data, size_t length, const char *id, message *out, const char **why) {
    if (len(data) != length) {
        p_free(data);
        if (why) *why = "the message cannot be read";
        return S_DAMAGED;
    }
    size_t split = 0;
    while (data[split] && !(data[split] == '\n' && data[split + 1] == '\n')) split++;
    if (!data[split]) {
        p_free(data);
        if (why) *why = "the message has no body";
        return S_DAMAGED;
    }
    data[split + 1] = 0;
    uint64_t body = 0;
    for (const char *d = field(data, "length"); *d >= '0' && *d <= '9' && body < MOST_FILE; d++) body = body * 10 + (uint64_t)(*d - '0');
    *out = (message){
        .file = data,
        .id = field(data, "id"),
        .time = field(data, "time"),
        .from = field(data, "from"),
        .to = field(data, "to"),
        .for_session = field(data, "for"),
        .kind = field(data, "kind"),
        .about = field(data, "about"),
        .urgency = field(data, "urgency"),
        .re = field(data, "re"),
        .subject = field(data, "subject"),
        .body = data + split + 2,
        .due = 0,
        .archived = false,
    };
    const char *due = field(data, "due");
    for (int digits = 0; *due >= '0' && *due <= '9' && digits < 17; digits++) out->due = out->due * 10 + (uint64_t)(*due++ - '0');
    bool due_ok = !*due || *due == '\n';
    const char *all[] = {out->id, out->time, out->from, out->to, out->for_session, out->kind, out->about, out->urgency, out->re, out->subject};
    for (size_t i = 0; i < sizeof all / sizeof *all; i++) if (*all[i]) cut(all[i]);
    if (!same(out->id, id) || !address_ok(out->from, false) || !address_ok(out->to, true)
        || !session_ok(out->for_session) || (*out->for_session && !address_ok(out->to, false))
        || !*out->subject || !due_ok || !time_ok(out->time) || (*out->re && !id_ok(out->re))
        || !one_of(out->kind, kinds) || !one_of(out->urgency, urgencies) || !*out->body || len(out->body) != body) {
        message_free(out);
        if (why) *why = "the message is not what its name says";
        return S_DAMAGED;
    }
    return S_OK;
}

s_result store_message_live(store *s, const char *id, message *out) {
    *out = (message){0};
    if (!id_ok(id)) return say(s, S_REFUSED, "that is not a message id");
    text at = {0};
    path(s, &at, id, "post");
    char *data = NULL;
    size_t length = 0;
    p_result result = at.failed ? P_FAILED : p_read(at.data, MOST_FILE, &data, &length);
    text_free(&at);
    if (result == P_MISSING) return say(s, S_REFUSED, "there is no such message");
    if (result != P_OK) return say(s, S_DAMAGED, "the message cannot be read");
    return store_parse_post(data, length, id, out, &s->why);
}

s_result store_message(store *s, const char *id, message *out) {
    s_result result = store_message_live(s, id, out);
    if (result == S_REFUSED && same(s->why, "there is no such message")) {
        s_result ar = archive_read_message(s, id, out);
        if (ar == S_OK) return S_OK;
        if (ar == S_DAMAGED) return S_DAMAGED;
        return say(s, S_REFUSED, "there is no such message");
    }
    return result;
}

bool is_job(const message *m) { return same(m->kind, "request") || same(m->kind, "handoff"); }

static bool lane_is_for(const message *m, const char *lane) {
    size_t n = len(m->to);
    if (same(m->to, lane)) return true;
    if (n <= 4 || !same(m->to + n - 4, "/all") || same(m->from, lane)) return false;
    for (size_t i = 0; i < n - 3; i++) /* the project and its slash */
        if (lane[i] != m->to[i]) return false;
    return true;
}

bool is_for(const message *m, actor me) {
    if (!lane_is_for(m, nz(me.lane))) return false;
    const char *for_s = nz(m->for_session);
    return !*for_s || same(for_s, nz(me.session));
}

static bool is_hex_lower(char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
}

bool store_valid_seen_event(const char *ev) {
    if (!ev || !starts(ev, "seen.")) return false;
    const char *who = ev + 5;
    size_t n = len(who);
    if (!n) return false;
    bool alert = n > 6 && same(who + n - 6, "~alert");
    size_t base_n = alert ? n - 6 : n;
    size_t plus_pos[2] = {0, 0};
    int plus = 0;
    for (size_t i = 0; i < base_n; i++) {
        if (who[i] == '/' || who[i] == '~') return false;
        if (who[i] == '+') {
            if (plus < 2) plus_pos[plus] = i;
            plus++;
        }
    }
    if (alert) {
        if (plus != 1 || base_n >= 72) return false;
        char addr[72];
        for (size_t i = 0; i < base_n; i++) addr[i] = who[i] == '+' ? '/' : who[i];
        addr[base_n] = 0;
        return address_ok(addr, false) && base_n > 6 && same(addr + base_n - 6, "/owner");
    }
    if (plus == 1) {
        if (base_n >= 72) return false;
        char addr[72];
        for (size_t i = 0; i < base_n; i++) addr[i] = who[i] == '+' ? '/' : who[i];
        addr[base_n] = 0;
        return address_ok(addr, false);
    }
    if (plus == 2) {
        size_t lane_n = plus_pos[1];
        if (lane_n >= 72 || base_n - lane_n - 1 != 64) return false;
        char addr[72];
        for (size_t i = 0; i < lane_n; i++) addr[i] = who[i] == '+' ? '/' : who[i];
        addr[lane_n] = 0;
        if (!address_ok(addr, false)) return false;
        for (size_t i = lane_n + 1; i < base_n; i++)
            if (!is_hex_lower(who[i])) return false;
        return true;
    }
    return false;
}

static void seen_name_ex(text *out, actor me, bool alert) {
    add(out, "seen.");
    for (const char *p = nz(me.lane); *p; p++) add_bytes(out, *p == '/' ? "+" : p, 1);
    if (alert) {
        add(out, "~alert");
    } else if (*nz(me.session)) {
        add(out, "+");
        add_sha256_hex(out, me.session, len(me.session));
    }
}

static s_result event_of(store *s, const char *id, const char *event, char **data);
static const char SEEN[] = "seen\n";

s_result store_was_seen(store *s, const char *id, actor me, bool alert, bool *seen_out) {
    if (seen_out) *seen_out = false;
    if (!address_ok(nz(me.lane), false) || (!alert && !session_ok(me.session)))
        return say(s, S_DAMAGED, "invalid seen actor");
    text event = {0};
    seen_name_ex(&event, me, alert);
    if (event.failed) return text_free(&event), say(s, S_DAMAGED, "out of memory");
    char *data = NULL;
    s_result er = event_of(s, id, event.data, &data);
    text_free(&event);
    if (er == S_NOTHING) return S_OK;
    if (er != S_OK) {
        p_free(data);
        return er;
    }
    bool ok = same(data, SEEN);
    p_free(data);
    if (!ok) return say(s, S_DAMAGED, "a seen mark is damaged");
    if (seen_out) *seen_out = true;
    return S_OK;
}

static s_result make_ex(store *s, const char *id, const char *event, const char *data, bool *published) {
    if (published) *published = false;
    text at = {0}, unique = {0};
    path(s, &at, id, event);
    add_number(&unique, p_tick(), 0);
    p_result result = at.failed || unique.failed ? P_FAILED : p_write(at.data, data, len(data), false, unique.data);
    text_free(&at);
    text_free(&unique);
    if (result == P_OK) {
        if (published) *published = true;
        return S_OK;
    }
    if (result == P_UNFLUSHED) {
        if (published) *published = true;
        return say(s, S_DAMAGED, "the store cannot be flushed");
    }
    if (result == P_EXISTS) return say(s, S_NOTHING, "it is already there");
    return say(s, S_DAMAGED, "the store cannot be written");
}

static s_result make(store *s, const char *id, const char *event, const char *data) {
    return make_ex(s, id, event, data, NULL);
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

static s_result live_post(store *s, const char *id) {
    char *data = NULL;
    s_result result = event_of(s, id, "post", &data);
    p_free(data);
    if (result == S_OK) return S_OK;
    if (result == S_NOTHING) return say(s, S_REFUSED, "it was archived");
    return say(s, S_DAMAGED, "the message cannot be read");
}

static s_result remove_event(store *s, const char *id, const char *event) {
    text at = {0};
    path(s, &at, id, event);
    p_result result = at.failed ? P_FAILED : p_remove(at.data);
    text_free(&at);
    if (result == P_OK || result == P_MISSING) return S_OK;
    return say(s, S_DAMAGED, "an archived step could not be removed");
}

/* The value of a field that must be there. */
static bool keep(char *to, size_t room, char *data, const char *name) {
    for (char *line = data; *line;) {
        char *end = line;
        while (*end && *end != '\n') end++;
        size_t skip = len(name) + 2;
        if (*end && starts(line, name) && line[skip - 2] == ':' && line[skip - 1] == ' ') {
            size_t n = (size_t)(end - line) - skip;
            if (n >= room) return false;
            memcpy(to, line + skip, n);
            to[n] = 0;
            return true;
        }
        line = *end ? end + 1 : end;
    }
    return false;
}

static void copy_to(char *to, const char *from) { memcpy(to, from, len(from) + 1); }

static void link_name(text *out, unsigned n) { add(out, "link."), add_number(out, n, 4); }

static const char *objection(const message *m, const job *now, actor me, change what);

/* A later step on the disk means that the one after the last we read is lost. */
static bool gap(const store *s, const char *id, unsigned links) {
    text first = {0}, next = {0};
    add(&first, id), add(&first, "."), link_name(&first, links);
    add(&next, id), add(&next, "."), link_name(&next, links + 1);
    size_t low = 0, high = s->count, n = len(id) + 6;
    while (!first.failed && low < high) {
        size_t mid = low + (high - low) / 2;
        if (compare(s->names[mid], first.data) <= 0) low = mid + 1;
        else high = mid;
    }
    bool found = first.failed || next.failed;
    if (!found && low < s->count) {
        found = true;
        for (size_t i = 0; i < n && found; i++) found = s->names[low][i] == first.data[i];
    }
    text_free(&first), text_free(&next);
    return found;
}

bool store_apply_step(const message *m, job *now, char *data) {
    static const char *const states[] = {"taken", "released", "done", "refused"};
    static const job_state after[] = {TAKEN, OPEN, DONE, REFUSED};
    char what[16], by[72], session[72], end[8], when[24];
    bool good = keep(what, sizeof what, data, "state") && keep(by, sizeof by, data, "by") && address_ok(by, false)
                && keep(session, sizeof session, data, "session") && keep(when, sizeof when, data, "time")
                && read_time(when, &now->when) && keep(end, sizeof end, data, "end") && same(end, "yes");
    int step = 0;
    while (good && step < 4 && !same(what, states[step])) step++;
    if (!good || step == 4 || objection(m, now, (actor){by, session}, (change)step)) return false;
    now->state = after[step];
    bool open = now->state == OPEN;
    copy_to(now->by, open ? "" : by), copy_to(now->session, open ? "" : session);
    return true;
}

/* Replays the chain from the disk, because others write meanwhile. A step counts
   only if its actor was allowed to make it: the rules are those of a new step. */
s_result store_job(store *s, const message *m, job *out) {
    if (m->archived) return archive_read_job(s, m, out);
    *out = (job){.state = OPEN};
    if (s->pause) s->pause(s->pause_user, "before-read");
    for (; out->links < MOST_LINKS; out->links++) {
        text name = {0};
        link_name(&name, out->links);
        char *data = NULL;
        s_result result = name.failed ? say(s, S_DAMAGED, "out of memory") : event_of(s, m->id, name.data, &data);
        text_free(&name);
        if (result == S_NOTHING) {
            if (s->pause) s->pause(s->pause_user, "read");
            s_result live = live_post(s, m->id);
            if (live != S_OK) return live;
            if (gap(s, m->id, out->links)) return say(s, S_DAMAGED, "a step of this job is missing");
            if (out->links == 0) {
                /* A killed keyed retry can leave a live .post with 0 live steps alongside a completed archived job:
                   surface the true archived terminal state rather than reporting a closed job as open. */
                s_result ar = archive_has_terminal_job(s->folder, m->id, NULL);
                if (ar == S_OK) return archive_read_job(s, m, out);
                if (ar == S_DAMAGED) return say(s, S_DAMAGED, "an archived job cannot be read");
            }
            return S_OK;
        }
        if (result != S_OK) return result;
        bool ok = store_apply_step(m, out, data);
        p_free(data);
        if (!ok) return say(s, S_DAMAGED, "a step of this job is damaged");
    }
    if (s->pause) s->pause(s->pause_user, "read");
    return live_post(s, m->id);
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
void clean(text *out, const char *in, bool one_line) {
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

static void fresh(text *id, uint64_t now) {
    uint64_t mixed = p_tick();
    add_number(id, now, 16), add(id, "-");
    for (int shift = 20; shift >= 0; shift -= 4) add_bytes(id, &"0123456789abcdef"[mixed >> (shift + 24) & 15], 1);
}

/* The id that this sender's key stands for: made once, so a retry finds it. */
static s_result reserve(store *s, const message *m, const char *key, text *id) {
    if (len(key) > 100) return say(s, S_REFUSED, "the key is too long");
    text whole = {0}, name = {0}, line = {0};
    clean(&line, key, true);
    add(&whole, m->from), add(&whole, " "), add(&whole, m->to), add(&whole, " "), add(&whole, line.data ? line.data : "");
    uint64_t hash = 14695981039346656037ull;
    for (const char *at = whole.data ? whole.data : ""; *at; at++) hash = (hash ^ (unsigned char)*at) * 1099511628211ull;
    add(&name, "key."), add_number(&name, hash, 20);
    add(&whole, "\n"), add(&whole, id->data);
    s_result result = S_DAMAGED;
    char *data = NULL;
    if (whole.failed || name.failed || line.failed) result = say(s, S_DAMAGED, "out of memory");
    else if ((result = make(s, "key", name.data, whole.data)) == S_NOTHING && (result = event_of(s, "key", name.data, &data)) == S_OK) {
        size_t n = 0;
        while (data[n] && data[n] != '\n') n++;
        bool mine = data[n] && id_ok(data + n + 1);
        for (size_t i = 0; mine && i <= n; i++) mine = data[i] == whole.data[i];
        if (mine) {
            text_free(id);
            add(id, data + n + 1);
            result = id->failed ? say(s, S_DAMAGED, "out of memory") : S_OK;
        } else {
            result = say(s, S_REFUSED, "that key is in use: choose another");
        }
    }
    p_free(data), text_free(&whole), text_free(&name), text_free(&line);
    return result;
}

static bool same_post_content(const message *there, const message *m, const char *about, const char *subject, const char *body) {
    return same(there->from, m->from) && same(there->to, m->to)
           && same(nz(there->for_session), nz(m->for_session))
           && same(there->kind, m->kind)
           && same(there->about, about ? about : "") && same(there->urgency, m->urgency)
           && same(there->re, m->re) && same(there->subject, subject) && same(there->body, body);
}

/* Read-only lookup of an existing key reservation (without creating one). */
static s_result find_reserved(store *s, const message *m, const char *key, text *id) {
    if (len(key) > 100) return say(s, S_REFUSED, "the key is too long");
    text whole = {0}, name = {0}, line = {0};
    clean(&line, key, true);
    add(&whole, m->from), add(&whole, " "), add(&whole, m->to), add(&whole, " "), add(&whole, line.data ? line.data : "");
    uint64_t hash = 14695981039346656037ull;
    for (const char *at = whole.data ? whole.data : ""; *at; at++) hash = (hash ^ (unsigned char)*at) * 1099511628211ull;
    add(&name, "key."), add_number(&name, hash, 20);
    s_result result = S_DAMAGED;
    char *data = NULL;
    if (whole.failed || name.failed || line.failed) result = say(s, S_DAMAGED, "out of memory");
    else if ((result = event_of(s, "key", name.data, &data)) == S_OK) {
        size_t n = 0;
        while (data[n] && data[n] != '\n') n++;
        bool mine = data[n] && n == whole.length && id_ok(data + n + 1);
        for (size_t i = 0; mine && i < n; i++) mine = data[i] == whole.data[i];
        if (mine) {
            text_free(id);
            add(id, data + n + 1);
            result = id->failed ? say(s, S_DAMAGED, "out of memory") : S_OK;
        } else {
            result = say(s, S_REFUSED, "that key is in use: choose another");
        }
    }
    p_free(data), text_free(&whole), text_free(&name), text_free(&line);
    return result;
}

s_result store_post(store *s, const message *m, const char *key, text *id) {
    const char *for_s = nz(m->for_session);
    if (!address_ok(m->from, false)) return say(s, S_REFUSED, "say who you are: --as project/lane");
    if (!address_ok(m->to, true)) return say(s, S_REFUSED, "the reader is project/lane, or project/all");
    if (!session_ok(for_s)) return say(s, S_REFUSED, "the target session name is invalid or too long");
    if (*for_s && !address_ok(m->to, false)) return say(s, S_REFUSED, "a message for one session must name a lane, not /all");
    if (!one_of(m->kind, kinds)) return say(s, S_REFUSED, "the kind is request, handoff, answer, finding or headsup");
    if (!one_of(m->urgency, urgencies)) return say(s, S_REFUSED, "the urgency is now, next or whenever");
    if (len(m->subject) > 120 || len(m->about) > 1000 || len(m->body) > MOST_BODY) return say(s, S_REFUSED, "the message is too long");
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
    if (subject.failed || about.failed || body.failed) {
        result = say(s, S_DAMAGED, "out of memory");
    } else if (blank || !body.length) {
        s->why = "a message needs a subject and a body";
    } else if (secret(subject.data) || secret(body.data) || secret(about.data ? about.data : "")) {
        s->why = "this looks like it contains a secret, so it was not sent";
    } else {
        /* At live admission capacity, still allow an exact retry of an already-reserved key (live or archived)
           without creating files, while refusing conflicting retries and genuinely new sends. */
        if (s->count >= store_max_names / 2) {
            if (*key) {
                s_result fr = find_reserved(s, m, key, id);
                if (fr == S_OK) {
                    message there = {0};
                    s_result mr = store_message(s, id->data, &there);
                    if (mr == S_OK) {
                        bool match = same_post_content(&there, m, about.data, subject.data, body.data);
                        message_free(&there);
                        text_free(&subject), text_free(&about), text_free(&body), text_free(&out);
                        return match ? S_OK : say(s, S_REFUSED, "that key stands for another message");
                    }
                    message_free(&there);
                    if (mr == S_DAMAGED) {
                        text_free(&subject), text_free(&about), text_free(&body), text_free(&out);
                        return S_DAMAGED;
                    }
                    text_free(id);
                } else if (fr != S_NOTHING) {
                    text_free(&subject), text_free(&about), text_free(&body), text_free(&out);
                    return fr;
                }
            }
            text_free(&subject), text_free(&about), text_free(&body), text_free(&out);
            return say(s, S_REFUSED, "the store is full: archive it first");
        }
        uint64_t now = p_now();
        fresh(id, now);
        result = id->failed ? say(s, S_DAMAGED, "out of memory") : *key ? reserve(s, m, key, id) : S_OK;
        if (result == S_OK && *key) {
            message arch = {0};
            s_result ar = archive_read_message(s, id->data, &arch);
            if (ar == S_OK) {
                if (!same_post_content(&arch, m, about.data, subject.data, body.data))
                    result = say(s, S_REFUSED, "that key stands for another message");
                message_free(&arch);
                text_free(&subject), text_free(&about), text_free(&body), text_free(&out);
                return result;
            }
            message_free(&arch);
            if (ar == S_DAMAGED) {
                text_free(&subject), text_free(&about), text_free(&body), text_free(&out);
                return S_DAMAGED;
            }
        }
        if (result == S_OK) {
            uint64_t stamp = now;
            if (*key && id->data) {
                uint64_t parsed = 0;
                const char *p = id->data;
                while (*p >= '0' && *p <= '9') parsed = parsed * 10 + (uint64_t)(*p++ - '0');
                if (*p == '-' && parsed) stamp = parsed;
            }
            add(&out, "id: "), add(&out, id->data), add(&out, "\ntime: "), add_time(&out, stamp);
            add(&out, "\nfrom: "), add(&out, m->from), add(&out, "\nto: "), add(&out, m->to);
            if (*for_s) add(&out, "\nfor: "), add(&out, for_s);
            add(&out, "\nkind: "), add(&out, m->kind);
            add(&out, "\nabout: "), add(&out, about.data ? about.data : ""), add(&out, "\nurgency: "), add(&out, m->urgency);
            add(&out, "\nre: "), add(&out, m->re), add(&out, "\ndue: ");
            if (m->due) add_number(&out, m->due, 0);
            add(&out, "\nlength: "), add_number(&out, body.length, 0);
            add(&out, "\nsubject: "), add(&out, subject.data), add(&out, "\n\n"), add(&out, body.data);
            if (*key && s->pause) s->pause(s->pause_user, "post-write");
            bool published = false;
            result = out.failed ? say(s, S_DAMAGED, "out of memory") : make_ex(s, id->data, "post", out.data, &published);
            if (published && *key) {
                if (s->pause) s->pause(s->pause_user, "after-post-write");
                /* Second look after publication: if the keyed message was archived while we raced, remove our
                   late live post so the closed job can never reopen as open. */
                message arch = {0};
                const char *write_why = s->why;
                s_result ar = archive_read_message(s, id->data, &arch);
                if (ar == S_OK) {
                    s_result rm = remove_event(s, id->data, "post");
                    bool match = same_post_content(&arch, m, about.data, subject.data, body.data);
                    message_free(&arch);
                    if (rm != S_OK) result = rm;
                    else if (!match) result = say(s, S_REFUSED, "that key stands for another message");
                    else if (result != S_OK) s->why = write_why;
                } else {
                    message_free(&arch);
                    if (ar == S_DAMAGED) result = S_DAMAGED;
                    else if (result != S_OK) s->why = write_why;
                }
            } else if (result == S_NOTHING && *key) {
                if (s->pause) s->pause(s->pause_user, "post-collision");
                message there = {0};
                result = store_message(s, id->data, &there);
                if (result == S_REFUSED && same(s->why, "there is no such message")) {
                    /* Live post was archived between create collision and read: check archive! */
                    s_result ar = archive_read_message(s, id->data, &there);
                    result = ar == S_OK ? S_OK : ar == S_NOTHING ? say(s, S_REFUSED, "there is no such message") : S_DAMAGED;
                }
                if (result == S_OK && !same_post_content(&there, m, about.data, subject.data, body.data))
                    result = say(s, S_REFUSED, "that key stands for another message");
                message_free(&there);
            } else if (result == S_NOTHING) {
                result = say(s, S_DAMAGED, "two messages got one id: send it again");
            }
        }
    }
    text_free(&subject), text_free(&about), text_free(&body), text_free(&out);
    return result;
}

/* Adds one link. S_NOTHING when someone else made that link first. */
static s_result add_link(store *s, const char *id, const job *now, actor me, const char *state, const char *note, bool *published) {
    if (published) *published = false;
    if (now->links >= MOST_LINKS - 2 && !same(state, "done") && !same(state, "refused")) return say(s, S_REFUSED, "this job changed hands too often: close it and ask again");
    if (!session_ok(me.session)) return say(s, S_REFUSED, "the session name is invalid or too long");
    text out = {0}, line = {0}, name = {0};
    clean(&line, note, true), link_name(&name, now->links);
    s_result result;
    if (line.length > MOST_NOTE) result = say(s, S_REFUSED, "the note or the session name is too long");
    else if (secret(line.data ? line.data : "")) result = say(s, S_REFUSED, "the note looks like it contains a secret");
    else {
        add(&out, "by: "), add(&out, me.lane), add(&out, "\ntime: "), add_time(&out, p_now()), add(&out, "\nsession: "), add(&out, me.session ? me.session : "");
        add(&out, "\nstate: "), add(&out, state), add(&out, "\nnote: "), add(&out, line.data ? line.data : ""), add(&out, "\nend: yes\n");
        bool full = out.failed || line.failed || name.failed;
        result = full ? say(s, S_DAMAGED, "out of memory") : make_ex(s, id, name.data, out.data, published);
    }
    text_free(&out), text_free(&line), text_free(&name);
    return result;
}

bool dest_owns(const char *lane, const message *m) {
    size_t n = len(lane);
    if (n <= 6 || !same(lane + n - 6, "/owner")) return false;
    for (size_t i = 0; i <= n - 6; i++)
        if (lane[i] != m->to[i]) return false;
    return true;
}

bool owns(const char *lane, const message *m) {
    size_t n = len(lane);
    if (n <= 6 || !same(lane + n - 6, "/owner")) return false;
    const char *sides[] = {m->to, m->from};
    for (int side = 0; side < 2; side++) {
        bool equal = true;
        for (size_t i = 0; i <= n - 6 && equal; i++) equal = lane[i] == sides[side][i];
        if (equal) return true;
    }
    return false;
}

static const char *objection(const message *m, const job *now, actor me, change what) {
    const char *for_s = nz(m->for_session);
    const char *my_s = nz(me.session);
    bool taker = now->state == TAKEN && same(now->by, me.lane) && (!*now->session || same(now->session, my_s));
    bool wrong_dest_session = *for_s && same(m->to, me.lane) && !same(for_s, my_s);
    if (now->state == DONE || now->state == REFUSED) return "it is already closed";
    switch (what) {
    case TAKE: return now->state == TAKEN ? "it is already taken" : is_for(m, me) ? NULL : "it is not for you";
    case RELEASE: return now->state != TAKEN ? "it is not taken" : taker || owns(me.lane, m) ? NULL : "someone else took it";
    case FINISH: return now->state != TAKEN ? "take it first" : taker ? NULL : "someone else took it";
    default:
        if (taker || (now->state == OPEN && is_for(m, me))) return NULL;
        if (!wrong_dest_session && (same(m->from, me.lane) || owns(me.lane, m))) return NULL;
        return "it is not yours to refuse";
    }
}

static s_result change_job(store *s, const char *id, actor me, change what, const char *note) {
    static const char *const states[] = {"taken", "released", "done", "refused"};
    if (!address_ok(me.lane, false)) return say(s, S_REFUSED, "say who you are: --as project/lane");
    message m;
    s_result result = store_message(s, id, &m);
    if (result != S_OK) return result;
    if (m.archived) result = say(s, S_REFUSED, "it was archived");
    else if (!is_job(&m)) result = say(s, S_REFUSED, "only a request or a handoff can be taken");
    if (result == S_OK) {
        /* Refuse if an archived terminal copy already exists; fail closed on archive I/O or parse errors (S_DAMAGED);
           allow ordinary in-progress archival (S_REFUSED = valid archived post without terminal links yet) to proceed. */
        s_result ar = archive_has_terminal_job(s->folder, id, NULL);
        if (ar == S_OK) result = say(s, S_REFUSED, "it was archived");
        else if (ar == S_DAMAGED) result = say(s, S_DAMAGED, "an archived job cannot be read");
    }
    unsigned before = MOST_LINKS + 1;
    while (result == S_OK) {
        job now;
        if ((result = store_job(s, &m, &now)) != S_OK) break;
        if (now.links == before) {
            result = say(s, S_DAMAGED, "the next step of this job is there but cannot be read");
            break;
        }
        before = now.links;
        const char *why = objection(&m, &now, me, what);
        if (why) {
            result = say(s, S_REFUSED, why);
        } else {
            if (s->pause) s->pause(s->pause_user, "write");
            bool published = false;
            result = add_link(s, id, &now, me, states[what], note, &published);
            if (published) {
                if (s->pause) s->pause(s->pause_user, "after-write");
                const char *write_why = s->why;
                s_result live = live_post(s, id);
                s_result ar = live == S_OK ? archive_has_terminal_job(s->folder, id, NULL) : S_NOTHING;
                if (live == S_REFUSED || ar == S_OK || ar == S_DAMAGED) {
                    text name = {0};
                    link_name(&name, now.links);
                    s_result rm = name.failed ? say(s, S_DAMAGED, "out of memory") : remove_event(s, id, name.data);
                    text_free(&name);
                    if (result == S_OK) {
                        if (ar == S_DAMAGED) result = rm == S_OK ? say(s, S_DAMAGED, "an archived job cannot be read") : rm;
                        else result = rm == S_OK ? say(s, S_REFUSED, "it was archived") : rm;
                    } else if (rm == S_OK) {
                        s->why = write_why;
                    }
                } else if (live != S_OK) {
                    result = live;
                } else if (result != S_OK) {
                    s->why = write_why;
                }
            }
        }
        if (result != S_NOTHING) break;
        result = S_OK; /* the chain grew: decide again from the new end */
    }
    message_free(&m);
    return result;
}

s_result store_take(store *s, const char *id, actor me) { return change_job(s, id, me, TAKE, ""); }
s_result store_release(store *s, const char *id, actor me) { return change_job(s, id, me, RELEASE, ""); }
s_result store_end(store *s, const char *id, actor me, bool done, const char *note) { return change_job(s, id, me, done ? FINISH : REFUSE, note); }

static s_result write_seen_ex(store *s, const char *id, actor me, bool alert) {
    if (!address_ok(nz(me.lane), false)) return say(s, S_REFUSED, "say who you are: --as project/lane");
    if (alert) {
        size_t n = len(me.lane);
        if (n <= 6 || !same(me.lane + n - 6, "/owner")) return say(s, S_REFUSED, "only an owner lane can acknowledge an alert");
    } else if (!session_ok(me.session)) {
        return say(s, S_REFUSED, "the session name is invalid or too long");
    }
    text event = {0};
    seen_name_ex(&event, me, alert);
    if (event.failed) return text_free(&event), say(s, S_DAMAGED, "out of memory");
    if (s->pause) s->pause(s->pause_user, "seen");
    bool published = false;
    s_result result = make_ex(s, id, event.data, SEEN, &published);
    if (published) {
        if (s->pause) s->pause(s->pause_user, "after-seen");
        const char *write_why = s->why;
        s_result live = live_post(s, id);
        if (live == S_REFUSED) {
            s_result rm = remove_event(s, id, event.data);
            if (result == S_OK) result = rm == S_OK ? say(s, S_REFUSED, "it was archived") : rm;
            else if (rm == S_OK) s->why = write_why;
        } else if (live != S_OK) {
            result = live;
        } else if (result != S_OK) {
            s->why = write_why;
        }
    } else if (result == S_NOTHING) {
        result = live_post(s, id);
    }
    text_free(&event);
    return result;
}

static s_result write_seen_archived_ex(store *s, const char *id, actor me, bool alert) {
    if (!address_ok(nz(me.lane), false)) return say(s, S_REFUSED, "say who you are: --as project/lane");
    if (alert) {
        size_t n = len(me.lane);
        if (n <= 6 || !same(me.lane + n - 6, "/owner")) return say(s, S_REFUSED, "only an owner lane can acknowledge an alert");
    } else if (!session_ok(me.session)) {
        return say(s, S_REFUSED, "the session name is invalid or too long");
    }
    text event = {0};
    seen_name_ex(&event, me, alert);
    if (event.failed) return text_free(&event), say(s, S_DAMAGED, "out of memory");
    s_result result = archive_write_seen(s, id, event.data);
    text_free(&event);
    return result;
}

s_result store_seen(store *s, const char *id, actor me) { return write_seen_ex(s, id, me, false); }
s_result store_seen_archived(store *s, const char *id, actor me) { return write_seen_archived_ex(s, id, me, false); }
s_result store_seen_alert(store *s, const char *id, const char *owner_lane) { return write_seen_ex(s, id, (actor){owner_lane, ""}, true); }
s_result store_seen_alert_archived(store *s, const char *id, const char *owner_lane) { return write_seen_archived_ex(s, id, (actor){owner_lane, ""}, true); }

s_result store_check(store *s, text *report) {
    s_result all = S_OK;
    for (size_t i = 0; i < s->count; i++) {
        const char *name = s->names[i], *dot = name;
        while (*dot && *dot != '.') dot++;
        char *id = copy(name, (size_t)(dot - name));
        const char *why = NULL;
        message m = {0};
        job now;
        if (!id) return say(s, S_DAMAGED, "out of memory");
        if (same(id, "key")) {}
        else {
            s_result mr = store_message_live(s, id, &m);
            if (mr == S_REFUSED && same(s->why, "there is no such message")) {
                if (!same(dot, ".post")) {
                    char *probe = NULL;
                    size_t plen = 0;
                    text evp = {0};
                    if (*dot) path(s, &evp, id, dot + 1);
                    p_result pr = !*dot || evp.failed ? P_FAILED : p_read(evp.data, MOST_FILE, &probe, &plen);
                    text_free(&evp);
                    if (pr == P_OK) {
                        if (!archive_is_clean_remnant(s->folder, id, dot + 1, probe, plen)) why = "there is no such message";
                        p_free(probe);
                    } else if (pr != P_MISSING) {
                        why = "an event cannot be read";
                    }
                }
            } else if (mr != S_OK) {
                why = s->why;
            } else if (same(dot, ".post") && is_job(&m)) {
                s_result jr = store_job(s, &m, &now);
                if (jr != S_OK && jr != S_REFUSED) why = s->why;
            } else if (starts(dot, ".seen.")) {
                if (s->pause) s->pause(s->pause_user, "check-seen");
                if (!store_valid_seen_event(dot + 1)) {
                    why = "a seen mark is damaged";
                } else {
                    char *data = NULL;
                    s_result er = event_of(s, id, dot + 1, &data);
                    if (er == S_NOTHING) {
                        s_result lp = live_post(s, id);
                        if (lp == S_OK) why = "a seen mark is damaged";
                        else if (lp != S_REFUSED) why = s->why;
                    } else if (er != S_OK || !same(data, SEEN)) {
                        why = "a seen mark is damaged";
                    }
                    p_free(data);
                }
            }
        }
        if (why) {
            text line = {0};
            clean(&line, name, true);
            add(report, line.data ? line.data : "?"), add(report, ": "), add(report, why), add(report, "\n");
            text_free(&line);
            all = say(s, S_DAMAGED, "the store has damage");
        }
        message_free(&m);
        p_free(id);
    }
    if (archive_check(s, report) != S_OK) all = say(s, S_DAMAGED, "the store has damage");
    return report->failed ? say(s, S_DAMAGED, "out of memory") : all;
}
