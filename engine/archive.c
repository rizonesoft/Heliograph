#include "archive.h"

static const uint64_t ARCHIVE_AGE_US = 30ull * 86400ull * 1000000ull;
static const char ARCHIVE_SEEN[] = "seen\n";

bool archive_month(const char *id, char out[8]) {
    if (!id || !*id) return false;
    uint64_t us = 0;
    size_t digits = 0;
    for (const char *p = id; *p && *p != '-'; p++) {
        if (*p < '0' || *p > '9' || digits >= 20) return false;
        uint64_t d = (uint64_t)(*p - '0');
        if (us > (UINT64_MAX - d) / 10) return false;
        us = us * 10 + d;
        digits++;
    }
    if (digits < 1) return false;
    text stamp = {0};
    add_time(&stamp, us);
    if (stamp.failed || stamp.length < 7) {
        text_free(&stamp);
        return false;
    }
    memcpy(out, stamp.data, 7);
    out[7] = 0;
    text_free(&stamp);
    return true;
}

static bool valid_id(const char *id) {
    size_t n = len(id);
    if (n < 8 || n > 40) return false;
    for (size_t i = 0; i < n; i++)
        if (!((id[i] >= '0' && id[i] <= '9') || (id[i] >= 'a' && id[i] <= 'f') || (id[i] == '-' && i))) return false;
    return true;
}

static void live_path(const char *folder, text *out, const char *id, const char *event) {
    add(out, folder), add(out, "/messages/"), add(out, id), add(out, "."), add(out, event);
}

static void arch_dir(const char *folder, const char *month, text *out) {
    add(out, folder), add(out, "/archive/"), add(out, month);
}

static void arch_path(const char *folder, const char *month, text *out, const char *id, const char *event) {
    arch_dir(folder, month, out), add(out, "/"), add(out, id), add(out, "."), add(out, event);
}

static void link_suffix(text *out, unsigned n) {
    add(out, "link."), add_number(out, n, 4);
}

static const char *msg_field(char *head, const char *name) {
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

static void msg_cut(const char *value) {
    char *at = (char *)value;
    while (*at && *at != '\n') at++;
    if (*at) *at = 0;
}

static bool one_of(const char *value, const char *const *list) {
    for (; *list; list++) if (same(value, *list)) return true;
    return false;
}

static s_result parse_post_bytes(char *data, size_t length, const char *id, message *out, const char **why) {
    static const char *const kinds[] = {"request", "handoff", "answer", "finding", "headsup", NULL};
    static const char *const urgencies[] = {"now", "next", "whenever", NULL};
    if (len(data) != length) {
        p_free(data);
        *why = "the message cannot be read";
        return S_DAMAGED;
    }
    size_t split = 0;
    while (data[split] && !(data[split] == '\n' && data[split + 1] == '\n')) split++;
    if (!data[split]) {
        p_free(data);
        *why = "the message has no body";
        return S_DAMAGED;
    }
    data[split + 1] = 0;
    uint64_t body = 0;
    for (const char *d = msg_field(data, "length"); *d >= '0' && *d <= '9' && body < MOST_FILE; d++) body = body * 10 + (uint64_t)(*d - '0');
    *out = (message){data, msg_field(data, "id"), msg_field(data, "time"), msg_field(data, "from"), msg_field(data, "to"), msg_field(data, "kind"),
                     msg_field(data, "about"), msg_field(data, "urgency"), msg_field(data, "re"), msg_field(data, "subject"), data + split + 2, 0};
    const char *due = msg_field(data, "due");
    for (int digits = 0; *due >= '0' && *due <= '9' && digits < 17; digits++) out->due = out->due * 10 + (uint64_t)(*due++ - '0');
    bool due_ok = !*due || *due == '\n';
    const char *all[] = {out->id, out->time, out->from, out->to, out->kind, out->about, out->urgency, out->re, out->subject};
    for (size_t i = 0; i < sizeof all / sizeof *all; i++) if (*all[i]) msg_cut(all[i]);
    uint64_t when = 0;
    if (!same(out->id, id) || !address_ok(out->from, false) || !address_ok(out->to, true) || !*out->subject
        || !due_ok || !read_time(out->time, &when) || (*out->re && !valid_id(out->re))
        || !one_of(out->kind, kinds) || !one_of(out->urgency, urgencies) || !*out->body || len(out->body) != body) {
        message_free(out);
        *why = "the message is not what its name says";
        return S_DAMAGED;
    }
    return S_OK;
}

s_result archive_read_message(store *s, const char *id, message *out) {
    *out = (message){0};
    char month[8];
    if (!valid_id(id) || !archive_month(id, month)) return S_NOTHING;
    text at = {0};
    arch_path(s->folder, month, &at, id, "post");
    char *data = NULL;
    size_t length = 0;
    p_result r = at.failed ? P_FAILED : p_read(at.data, MOST_FILE, &data, &length);
    text_free(&at);
    if (r == P_MISSING) return S_NOTHING;
    if (r != P_OK) {
        s->why = "an archived message cannot be read";
        return S_DAMAGED;
    }
    return parse_post_bytes(data, length, id, out, &s->why);
}

static bool keep_field(char *to, size_t room, char *data, const char *name) {
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

static bool valid_step_transition(const message *m, const job *now, const char *by, const char *session, int step) {
    bool taker = now->state == TAKEN && same(now->by, by) && (!*now->session || same(now->session, session));
    if (now->state == DONE || now->state == REFUSED) return false;
    switch (step) {
    case 0: return now->state != TAKEN && is_for(m, by);
    case 1: return now->state == TAKEN && (taker || owns(by, m));
    case 2: return now->state == TAKEN && taker;
    default: return taker || same(m->from, by) || owns(by, m) || (now->state == OPEN && is_for(m, by));
    }
}

bool archive_has_terminal_job(const char *folder, const char *id) {
    char month[8];
    if (!valid_id(id) || !archive_month(id, month)) return false;
    store tmp = {.folder = folder};
    message m = {0};
    if (archive_read_message(&tmp, id, &m) != S_OK || !is_job(&m)) {
        message_free(&m);
        return false;
    }
    static const char *const states[] = {"taken", "released", "done", "refused"};
    static const job_state after[] = {TAKEN, OPEN, DONE, REFUSED};
    job now = {.state = OPEN};
    bool ok = true;
    for (; now.links < MOST_LINKS; now.links++) {
        text ev = {0}, at = {0};
        link_suffix(&ev, now.links);
        if (!ev.failed) arch_path(folder, month, &at, id, ev.data);
        char *data = NULL;
        size_t length = 0;
        p_result r = ev.failed || at.failed ? P_FAILED : p_read(at.data, MOST_FILE, &data, &length);
        text_free(&ev), text_free(&at);
        if (r == P_MISSING) break;
        if (r != P_OK || len(data) != length) {
            if (r == P_OK) p_free(data);
            ok = false;
            break;
        }
        char what[16], by[72], session[72], end[8], when[24];
        bool good = keep_field(what, sizeof what, data, "state") && keep_field(by, sizeof by, data, "by") && address_ok(by, false)
                    && keep_field(session, sizeof session, data, "session") && keep_field(when, sizeof when, data, "time")
                    && read_time(when, &now.when) && keep_field(end, sizeof end, data, "end") && same(end, "yes");
        p_free(data);
        int step = 0;
        while (good && step < 4 && !same(what, states[step])) step++;
        if (!good || step == 4 || !valid_step_transition(&m, &now, by, session, step)) {
            ok = false;
            break;
        }
        now.state = after[step];
        bool open = now.state == OPEN;
        const char *nb = open ? "" : by, *ns = open ? "" : session;
        memcpy(now.by, nb, len(nb) + 1);
        memcpy(now.session, ns, len(ns) + 1);
    }
    message_free(&m);
    return ok && (now.state == DONE || now.state == REFUSED);
}

/* Durable copy of src -> dst; refuses on differing existing dst, syncs identical existing dst. */
static s_result copy_durable(const char *src, const char *dst, const char **why) {
    char *data = NULL;
    size_t length = 0;
    p_result r = p_read(src, MOST_FILE, &data, &length);
    if (r == P_MISSING) {
        /* A concurrent peer may have already archived and removed src: verify dst exists. */
        char *there = NULL;
        size_t tlen = 0;
        p_result dr = p_read(dst, MOST_FILE, &there, &tlen);
        p_free(there);
        if (dr == P_OK) return p_sync(dst) == P_OK ? S_NOTHING : (*why = "an archive copy cannot be flushed", S_DAMAGED);
        *why = "a live file vanished before its archive copy was present";
        return S_DAMAGED;
    }
    if (r != P_OK) {
        *why = "a live file cannot be read";
        return S_DAMAGED;
    }
    text unique = {0};
    add_number(&unique, p_tick(), 0);
    p_result w = unique.failed ? P_FAILED : p_write(dst, data, length, false, unique.data);
    text_free(&unique);
    if (w == P_OK) {
        p_free(data);
        return S_OK;
    }
    if (w == P_EXISTS) {
        char *there = NULL;
        size_t tlen = 0;
        p_result dr = p_read(dst, MOST_FILE, &there, &tlen);
        bool equal = dr == P_OK && tlen == length;
        for (size_t i = 0; equal && i < length; i++) equal = there[i] == data[i];
        p_free(there);
        p_free(data);
        if (!equal) {
            *why = "archive copy differs from live file";
            return S_DAMAGED;
        }
        if (p_sync(dst) != P_OK) {
            *why = "an archive copy cannot be flushed";
            return S_DAMAGED;
        }
        return S_OK;
    }
    p_free(data);
    *why = w == P_UNFLUSHED ? "an archive copy cannot be flushed" : "an archive copy cannot be written";
    return S_DAMAGED;
}

typedef struct {
    char **items;
    size_t count, capacity, cap_limit;
    bool failed;
} str_list;

static void list_free(str_list *l) {
    for (size_t i = 0; i < l->count; i++) p_free(l->items[i]);
    p_free(l->items);
    *l = (str_list){0};
}

static bool list_push(str_list *l, const char *s, size_t n) {
    if (l->cap_limit && l->count >= l->cap_limit) return false;
    if (l->count == l->capacity) {
        size_t cap = l->capacity ? l->capacity * 2 : 64;
        if (l->cap_limit && cap > l->cap_limit) cap = l->cap_limit;
        char **next = p_alloc(cap * sizeof *next);
        if (!next) return l->failed = true, false;
        if (l->count) memcpy(next, l->items, l->count * sizeof *next);
        p_free(l->items);
        l->items = next, l->capacity = cap;
    }
    if (!(l->items[l->count] = copy(s, n))) return l->failed = true, false;
    l->count++;
    return true;
}

static void sift_str(char **a, size_t i, size_t n) {
    for (size_t child; (child = 2 * i + 1) < n; i = child) {
        if (child + 1 < n && compare(a[child], a[child + 1]) < 0) child++;
        if (compare(a[i], a[child]) >= 0) return;
        char *swap = a[i];
        a[i] = a[child], a[child] = swap;
    }
}

static void sort_list(str_list *l) {
    for (size_t i = l->count / 2; i-- > 0;) sift_str(l->items, i, l->count);
    for (size_t n = l->count; n > 1;) {
        char *swap = l->items[0];
        l->items[0] = l->items[--n], l->items[n] = swap;
        sift_str(l->items, 0, n);
    }
}

typedef struct {
    const char *id;
    size_t id_len;
    str_list seen;
    str_list links;
    bool has_other;
} id_events;

static bool gather_id_events(void *user, const char *name) {
    id_events *e = user;
    size_t n = len(name);
    if (n > 5 && same(name + n - 5, ".part")) return true;
    if (n <= e->id_len + 1 || name[e->id_len] != '.') return true;
    for (size_t i = 0; i < e->id_len; i++) if (name[i] != e->id[i]) return true;
    const char *ev = name + e->id_len + 1;
    if (same(ev, "post")) return true;
    if (starts(ev, "seen.")) list_push(&e->seen, ev, len(ev));
    else if (starts(ev, "link.")) list_push(&e->links, ev, len(ev));
    else e->has_other = true;
    return !e->seen.failed && !e->links.failed;
}

static bool collect_id_events(const char *folder, const char *id, id_events *e) {
    *e = (id_events){.id = id, .id_len = len(id)};
    text dir = {0};
    add(&dir, folder), add(&dir, "/messages");
    p_result r = dir.failed ? P_FAILED : p_list(dir.data, gather_id_events, e);
    text_free(&dir);
    if (r != P_OK || e->seen.failed || e->links.failed) return false;
    sort_list(&e->seen);
    sort_list(&e->links);
    return true;
}

static void free_id_events(id_events *e) {
    list_free(&e->seen);
    list_free(&e->links);
}

static void add_report(text *report, const char *id, const char *why) {
    if (!report) return;
    text clean_id = {0};
    clean(&clean_id, id, true);
    add(report, clean_id.data ? clean_id.data : "?"), add(report, ": "), add(report, why ? why : "damaged"), add(report, "\n");
    text_free(&clean_id);
}

/* Ensures archive/ and archive/<month> exist durably. */
static bool ensure_archive_month(const char *folder, const char *month) {
    text a = {0}, m = {0};
    add(&a, folder), add(&a, "/archive");
    arch_dir(folder, month, &m);
    bool ok = !a.failed && !m.failed && p_folder(a.data) == P_OK && p_folder(m.data) == P_OK;
    text_free(&a), text_free(&m);
    return ok;
}

/* Copies valid late .seen.* marks to archive then removes live .seen.*, then removes live .link.* last-to-first. */
static s_result sweep_live_tail(const char *folder, const char *month, const char *id,
                                void (*pause)(void *user, const char *stage), void *pause_user, const char **why) {
    id_events ev;
    if (!collect_id_events(folder, id, &ev)) {
        free_id_events(&ev);
        *why = "the store cannot be read";
        return S_DAMAGED;
    }
    s_result res = S_OK;
    for (size_t i = 0; res == S_OK && i < ev.seen.count; i++) {
        text src = {0}, dst = {0};
        live_path(folder, &src, id, ev.seen.items[i]);
        arch_path(folder, month, &dst, id, ev.seen.items[i]);
        char *data = NULL;
        size_t length = 0;
        p_result sr = src.failed || dst.failed ? P_FAILED : p_read(src.data, MOST_FILE, &data, &length);
        if (sr == P_OK) {
            bool valid_seen = length == len(ARCHIVE_SEEN) && same(data, ARCHIVE_SEEN);
            p_free(data);
            if (valid_seen) {
                s_result cr = copy_durable(src.data, dst.data, why);
                if (cr == S_DAMAGED) res = S_DAMAGED;
            }
            if (res == S_OK) {
                if (pause) pause(pause_user, "before-remove-seen");
                p_result rm = p_remove(src.data);
                if (rm != P_OK && rm != P_MISSING) *why = "a seen mark could not be removed", res = S_DAMAGED;
            }
        } else if (sr != P_MISSING) {
            *why = "a seen mark cannot be read";
            res = S_DAMAGED;
        }
        text_free(&src), text_free(&dst);
    }
    if (res == S_OK && pause) pause(pause_user, "after-remove-seen");
    for (size_t i = ev.links.count; res == S_OK && i-- > 0;) {
        text src = {0};
        live_path(folder, &src, id, ev.links.items[i]);
        if (pause) pause(pause_user, "before-remove-link");
        p_result rm = src.failed ? P_FAILED : p_remove(src.data);
        text_free(&src);
        if (rm != P_OK && rm != P_MISSING) *why = "a step could not be removed", res = S_DAMAGED;
    }
    free_id_events(&ev);
    return res;
}

typedef struct {
    str_list ids;
    const char *after; /* resume strictly after this ID on next bounded batch */
    bool truncated;
} batch_ctx;

static bool list_contains(const str_list *l, const char *s, size_t n) {
    for (size_t i = 0; i < l->count; i++)
        if (len(l->items[i]) == n && !compare(l->items[i], s)) return true;
    return false;
}

static bool gather_batch(void *user, const char *name) {
    batch_ctx *b = user;
    size_t n = len(name);
    if (n > 5 && same(name + n - 5, ".part")) return true;
    const char *dot = name;
    while (*dot && *dot != '.') dot++;
    if (!*dot) return true;
    size_t id_len = (size_t)(dot - name);
    if (id_len == 3 && starts(name, "key")) return true;
    char *id = copy(name, id_len);
    if (!id) return b->ids.failed = true, false;
    if (b->after && compare(id, b->after) <= 0) {
        p_free(id);
        return true;
    }
    if (!list_contains(&b->ids, id, id_len)) {
        if (b->ids.count < b->ids.cap_limit) {
            if (!list_push(&b->ids, id, id_len)) {
                p_free(id);
                return false;
            }
        } else {
            b->truncated = true;
            /* Keep the cap_limit lexicographically smallest IDs > after so unsorted readdir never skips any ID. */
            size_t max_i = 0;
            for (size_t i = 1; i < b->ids.count; i++)
                if (compare(b->ids.items[i], b->ids.items[max_i]) > 0) max_i = i;
            if (compare(id, b->ids.items[max_i]) < 0) {
                p_free(b->ids.items[max_i]);
                b->ids.items[max_i] = id;
                return true;
            }
        }
    }
    p_free(id);
    return true;
}

static s_result archive_one(const char *folder, const char *id, uint64_t now,
                            bool *did_move, bool *peer_moved, text *report,
                            void (*pause)(void *user, const char *stage), void *pause_user) {
    *did_move = false;
    *peer_moved = false;
    const char *why = NULL;
    if (!valid_id(id)) {
        add_report(report, id, "that is not a message id");
        return S_DAMAGED;
    }
    char month[8];
    if (!archive_month(id, month)) {
        add_report(report, id, "the message id has no valid date");
        return S_DAMAGED;
    }
    text post_p = {0};
    live_path(folder, &post_p, id, "post");
    char *pdata = NULL;
    size_t plen = 0;
    p_result pr = post_p.failed ? P_FAILED : p_read(post_p.data, MOST_FILE, &pdata, &plen);
    if (pr == P_MISSING) {
        text_free(&post_p);
        /* Live post is proven absent: sweep live tail ONLY if archive holds a validated terminal job. */
        if (!archive_has_terminal_job(folder, id)) return S_NOTHING;
        if (!ensure_archive_month(folder, month)) {
            add_report(report, id, "archive folder cannot be created");
            return S_DAMAGED;
        }
        s_result sw = sweep_live_tail(folder, month, id, pause, pause_user, &why);
        if (sw != S_OK) {
            add_report(report, id, why);
            return S_DAMAGED;
        }
        *did_move = true;
        return S_OK;
    }
    if (pr != P_OK) {
        text_free(&post_p);
        add_report(report, id, "the message cannot be read");
        return S_DAMAGED;
    }
    message m = {0};
    if (parse_post_bytes(pdata, plen, id, &m, &why) != S_OK) {
        text_free(&post_p);
        add_report(report, id, why);
        return S_DAMAGED;
    }
    if (!is_job(&m)) {
        message_free(&m);
        text_free(&post_p);
        return S_NOTHING;
    }

    /* Fast path: replay links directly before scanning the directory so open/recent jobs never re-list messages/. */
    store fast = {.folder = folder};
    job j = {0};
    s_result jr = store_job(&fast, &m, &j);
    if (jr == S_REFUSED) {
        message_free(&m);
        text_free(&post_p);
        *peer_moved = true;
        return S_OK;
    }
    if (jr != S_OK) {
        message_free(&m);
        text_free(&post_p);
        add_report(report, id, fast.why ? fast.why : "a step of this job is damaged");
        return S_DAMAGED;
    }
    if ((j.state != DONE && j.state != REFUSED) || j.when > now || now - j.when < ARCHIVE_AGE_US) {
        /* Still verify no link gap exists when a job looks open/taken/recent only if links were read? No: gap()
           catches missing link.0000 when link.0001 exists; for archiving, an eligible job re-verifies with full
           link list below, and ineligible jobs are checked by helio check. Wait: check if a damaged job with a
           missing link.0000 must be named by helio archive -> check link.0001 if j.links == 0! */
        text probe_ev = {0}, probe_p = {0};
        link_suffix(&probe_ev, j.links + 1);
        if (!probe_ev.failed) live_path(folder, &probe_p, id, probe_ev.data);
        char *pd = NULL;
        size_t pl = 0;
        p_result pr2 = probe_ev.failed || probe_p.failed ? P_MISSING : p_read(probe_p.data, 64, &pd, &pl);
        p_free(pd);
        text_free(&probe_ev), text_free(&probe_p);
        message_free(&m);
        text_free(&post_p);
        if (pr2 != P_MISSING) {
            add_report(report, id, "a step of this job is missing");
            return S_DAMAGED;
        }
        return S_NOTHING;
    }

    /* Eligible closed job: gather all live links and seen marks and re-verify chain with full gap() check. */
    id_events ev;
    if (!collect_id_events(folder, id, &ev)) {
        free_id_events(&ev);
        message_free(&m);
        text_free(&post_p);
        add_report(report, id, "the store cannot be read");
        return S_DAMAGED;
    }
    store mini = {.folder = folder, .names = NULL, .count = 0};
    str_list link_names = {0};
    for (size_t i = 0; i < ev.links.count; i++) {
        text full = {0};
        add(&full, id), add(&full, "."), add(&full, ev.links.items[i]);
        if (full.failed || !list_push(&link_names, full.data, full.length)) link_names.failed = true;
        text_free(&full);
    }
    if (link_names.failed) {
        list_free(&link_names);
        free_id_events(&ev);
        message_free(&m);
        text_free(&post_p);
        add_report(report, id, "out of memory");
        return S_DAMAGED;
    }
    sort_list(&link_names);
    mini.names = link_names.items;
    mini.count = link_names.count;
    jr = store_job(&mini, &m, &j);
    list_free(&link_names);
    if (jr == S_REFUSED) {
        free_id_events(&ev);
        message_free(&m);
        text_free(&post_p);
        *peer_moved = true;
        return S_OK;
    }
    if (jr != S_OK) {
        free_id_events(&ev);
        message_free(&m);
        text_free(&post_p);
        add_report(report, id, mini.why ? mini.why : "a step of this job is damaged");
        return S_DAMAGED;
    }

    /* Validate all live seen marks before copying anything. */
    for (size_t i = 0; i < ev.seen.count; i++) {
        text sp = {0};
        live_path(folder, &sp, id, ev.seen.items[i]);
        char *sd = NULL;
        size_t sl = 0;
        p_result sr = sp.failed ? P_FAILED : p_read(sp.data, MOST_FILE, &sd, &sl);
        text_free(&sp);
        if (sr == P_MISSING) continue;
        bool ok_seen = sr == P_OK && sl == len(ARCHIVE_SEEN) && same(sd, ARCHIVE_SEEN);
        p_free(sd);
        if (!ok_seen) {
            free_id_events(&ev);
            message_free(&m);
            text_free(&post_p);
            add_report(report, id, "a seen mark is damaged");
            return S_DAMAGED;
        }
    }

    if (!ensure_archive_month(folder, month)) {
        free_id_events(&ev);
        message_free(&m);
        text_free(&post_p);
        add_report(report, id, "archive folder cannot be created");
        return S_DAMAGED;
    }

    /* Phase 1: Copy post, all steps, and all seen marks to archive/<YYYY-MM>/ before removing anything. */
    text dst_post = {0};
    arch_path(folder, month, &dst_post, id, "post");
    if (pause) pause(pause_user, "before-copy-post");
    s_result cr = dst_post.failed ? (why = "out of memory", S_DAMAGED) : copy_durable(post_p.data, dst_post.data, &why);
    text_free(&dst_post);
    if (cr == S_DAMAGED) {
        free_id_events(&ev);
        message_free(&m);
        text_free(&post_p);
        add_report(report, id, why);
        return S_DAMAGED;
    }
    if (pause) pause(pause_user, "after-copy-post");

    for (unsigned k = 0; k < j.links; k++) {
        text lnm = {0}, lsrc = {0}, ldst = {0};
        link_suffix(&lnm, k);
        if (!lnm.failed) {
            live_path(folder, &lsrc, id, lnm.data);
            arch_path(folder, month, &ldst, id, lnm.data);
        }
        if (pause) pause(pause_user, "before-copy-link");
        s_result lr = lnm.failed || lsrc.failed || ldst.failed ? (why = "out of memory", S_DAMAGED) : copy_durable(lsrc.data, ldst.data, &why);
        text_free(&lnm), text_free(&lsrc), text_free(&ldst);
        if (lr == S_DAMAGED) {
            free_id_events(&ev);
            message_free(&m);
            text_free(&post_p);
            add_report(report, id, why);
            return S_DAMAGED;
        }
    }
    if (pause) pause(pause_user, "after-copy-links");

    for (size_t i = 0; i < ev.seen.count; i++) {
        text ssrc = {0}, sdst = {0};
        live_path(folder, &ssrc, id, ev.seen.items[i]);
        arch_path(folder, month, &sdst, id, ev.seen.items[i]);
        if (pause) pause(pause_user, "before-copy-seen");
        s_result sr = ssrc.failed || sdst.failed ? (why = "out of memory", S_DAMAGED) : copy_durable(ssrc.data, sdst.data, &why);
        text_free(&ssrc), text_free(&sdst);
        if (sr == S_DAMAGED) {
            free_id_events(&ev);
            message_free(&m);
            text_free(&post_p);
            add_report(report, id, why);
            return S_DAMAGED;
        }
    }
    free_id_events(&ev);
    message_free(&m);

    /* Phase 2: Remove post first, then seen marks (preserving any late seen marks!), then steps last-to-first. */
    if (pause) pause(pause_user, "before-remove-post");
    p_result rm_post = p_remove(post_p.data);
    text_free(&post_p);
    if (rm_post != P_OK && rm_post != P_MISSING) {
        add_report(report, id, "the post could not be removed");
        return S_DAMAGED;
    }
    if (pause) pause(pause_user, "after-remove-post");

    s_result sw = sweep_live_tail(folder, month, id, pause, pause_user, &why);
    if (sw != S_OK) {
        add_report(report, id, why);
        return S_DAMAGED;
    }
    if (rm_post == P_MISSING) *peer_moved = true;
    else *did_move = true;
    return S_OK;
}

s_result archive_store(const char *folder, size_t limit, size_t *moved, text *report,
                       void (*pause)(void *user, const char *stage), void *pause_user) {
    if (moved) *moved = 0;
    size_t total_moved = 0, total_peer = 0;
    bool had_damage = false;
    text msg_dir = {0};
    add(&msg_dir, folder), add(&msg_dir, "/messages");
    if (msg_dir.failed) return S_DAMAGED;

    uint64_t now = p_now();
    char *cursor = NULL;
    for (;;) {
        batch_ctx b = {.ids = {.cap_limit = 2048}, .after = cursor, .truncated = false};
        p_result lr = p_list(msg_dir.data, gather_batch, &b);
        if (lr == P_MISSING) {
            list_free(&b.ids);
            p_free(cursor);
            text_free(&msg_dir);
            if (report) add(report, "there is no store here: run helio init\n");
            return S_DAMAGED;
        }
        if ((lr != P_OK && !b.truncated) || b.ids.failed) {
            list_free(&b.ids);
            p_free(cursor);
            text_free(&msg_dir);
            if (report) add(report, "the store cannot be read\n");
            return S_DAMAGED;
        }
        if (b.ids.count == 0) {
            list_free(&b.ids);
            break;
        }
        sort_list(&b.ids);
        for (size_t i = 0; i < b.ids.count; i++) {
            bool did = false, peer = false;
            s_result r = archive_one(folder, b.ids.items[i], now, &did, &peer, report, pause, pause_user);
            if (r == S_DAMAGED) had_damage = true;
            if (did) total_moved++;
            if (peer) total_peer++;
            if (limit && total_moved >= limit) break;
        }
        if ((limit && total_moved >= limit) || !b.truncated) {
            list_free(&b.ids);
            break;
        }
        char *next_cursor = copy(b.ids.items[b.ids.count - 1], len(b.ids.items[b.ids.count - 1]));
        list_free(&b.ids);
        p_free(cursor);
        cursor = next_cursor;
        if (!cursor) {
            had_damage = true;
            break;
        }
    }
    p_free(cursor);
    text_free(&msg_dir);
    if (moved) *moved = total_moved;
    if (had_damage) return S_DAMAGED;
    return (total_moved > 0 || total_peer > 0) ? S_OK : S_NOTHING;
}

s_result archive_daily(const char *folder, void (*pause)(void *user, const char *stage), void *pause_user) {
    text day = {0}, sdir = {0}, mark = {0}, unique = {0};
    add_time(&day, p_now());
    if (day.data) day.data[day.length = 10] = 0;
    add(&sdir, folder), add(&sdir, "/summary");
    add(&mark, sdir.data ? sdir.data : ""), add(&mark, "/"), add(&mark, day.data ? day.data : ""), add(&mark, ".archived");
    add_number(&unique, p_tick(), 0);
    bool full = day.failed || sdir.failed || mark.failed || unique.failed;
    char *there = NULL;
    size_t tlen = 0;
    p_result got = full ? P_FAILED : p_read(mark.data, 64, &there, &tlen);
    p_free(there);
    s_result res = S_OK;
    if (got == P_MISSING) {
        size_t moved = 0;
        text report = {0};
        s_result ar = archive_store(folder, 200, &moved, &report, pause, pause_user);
        text_free(&report);
        if (ar == S_DAMAGED) {
            res = S_DAMAGED;
        } else {
            text body = {0};
            add_number(&body, (uint64_t)moved, 0), add(&body, "\n");
            p_result wr = body.failed || p_folder(sdir.data) != P_OK ? P_FAILED : p_write(mark.data, body.data, body.length, false, unique.data);
            text_free(&body);
            if (wr != P_OK && wr != P_EXISTS) res = S_DAMAGED;
        }
    } else if (got != P_OK) {
        res = S_DAMAGED;
    }
    text_free(&day), text_free(&sdir), text_free(&mark), text_free(&unique);
    return res;
}
