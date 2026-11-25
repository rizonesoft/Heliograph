#include "watch.h"

enum { MOST_PATHS = 2000 };

bool is_overdue(const message *m, const job *now) {
    return m->due && (now->state == OPEN || now->state == TAKEN) && m->due < p_now();
}

bool is_silent(const session_info *sessions, size_t session_count, const job *now, uint64_t clock_now, uint64_t silent_us) {
    if (!now || now->state != TAKEN || !*now->by || !silent_us) return false;
    uint64_t last = now->when;
    if (*now->session) {
        for (size_t i = 0; i < session_count; i++) {
            if (*now->repository && !same(sessions[i].repository, now->repository)) continue;
            if (same(sessions[i].lane, now->by) && same(sessions[i].session, now->session)) {
                if (sessions[i].when > last) last = sessions[i].when;
            }
        }
    } else {
        for (size_t i = 0; i < session_count; i++) {
            if (*now->repository && !same(sessions[i].repository, now->repository)) continue;
            if (same(sessions[i].lane, now->by) && sessions[i].when > last)
                last = sessions[i].when;
        }
    }
    return clock_now > last && (clock_now - last) >= silent_us;
}

bool is_session_silent(const session_info *sessions, size_t session_count, const char *lane, const char *session,
                       uint64_t since_us, uint64_t clock_now, uint64_t silent_us) {
    if (!lane || !*lane || !session || !*session || !silent_us) return false;
    uint64_t last = since_us;
    for (size_t i = 0; i < session_count; i++) {
        if (same(sessions[i].lane, lane) && same(sessions[i].session, session)) {
            if (sessions[i].when > last) last = sessions[i].when;
            break;
        }
    }
    return clock_now > last && (clock_now - last) >= silent_us;
}

typedef struct {
    session_cache *cache;
    bool oom;
} sess_collect;

static bool collect_session_cb(void *user, const session_info *info) {
    sess_collect *sc = user;
    session_cache *c = sc->cache;
    if (c->count == c->cap) {
        size_t next = c->cap ? c->cap * 2 : 16;
        session_info *grown = p_alloc(next * sizeof *grown);
        if (!grown) { sc->oom = true; return false; }
        for (size_t i = 0; i < c->count; i++) grown[i] = c->items[i];
        p_free(c->items);
        c->items = grown, c->cap = next;
    }
    c->items[c->count++] = *info;
    return true;
}

static bool ensure_session_loaded(store *s, session_cache *c, const char *lane, const char *session) {
    const char *sess = session ? session : "";
    for (size_t i = 0; i < c->query_count; i++) {
        if (same(c->queries[i].lane, lane) && same(c->queries[i].session, sess))
            return !c->queries[i].failed;
    }
    bool failed = false;
    if (*sess) {
        session_info info = {0};
        s_result sr = place_session(s, lane, sess, &info);
        if (sr == S_DAMAGED) failed = true;
        else if (sr == S_OK) {
            sess_collect col = {c, false};
            collect_session_cb(&col, &info);
            if (col.oom) failed = true;
        }
    } else {
        sess_collect col = {c, false};
        p_result pr = place_lane_sessions(s, lane, collect_session_cb, &col);
        if (col.oom || (pr != P_OK && pr != P_MISSING)) failed = true;
    }
    if (c->query_count == c->query_cap) {
        size_t next = c->query_cap ? c->query_cap * 2 : 8;
        session_query_entry *grown = p_alloc(next * sizeof *grown);
        if (grown) {
            for (size_t i = 0; i < c->query_count; i++) grown[i] = c->queries[i];
            p_free(c->queries);
            c->queries = grown, c->query_cap = next;
        }
    }
    if (c->query_count < c->query_cap) {
        session_query_entry *q = &c->queries[c->query_count++];
        size_t i = 0;
        for (; i + 1 < sizeof q->lane && lane[i]; i++) q->lane[i] = lane[i];
        q->lane[i] = 0;
        i = 0;
        for (; i + 1 < sizeof q->session && sess[i]; i++) q->session[i] = sess[i];
        q->session[i] = 0;
        q->failed = failed;
    }
    return !failed;
}

void sessions_cache_free(session_cache *c) {
    p_free(c->items);
    p_free(c->queries);
    p_free(c->thresholds);
    *c = (session_cache){0};
}

static void job_project(const char *address, char *out, size_t room) {
    if (!room) return;
    size_t i = 0;
    if (address)
        while (i + 1 < room && address[i] && address[i] != '/') out[i] = address[i], i++;
    out[i] = 0;
}

static p_result cached_silent_us(const store *s, const place *here, session_cache *sc, const char *project, uint64_t *out_us) {
    for (size_t i = 0; i < sc->thresh_count; i++) {
        if (same(sc->thresholds[i].project, project)) {
            if (sc->thresholds[i].failed) return P_FAILED;
            *out_us = sc->thresholds[i].us;
            return P_OK;
        }
    }
    uint64_t us = 0;
    p_result r = place_silent_us(s, here, project, &us);
    if (sc->thresh_count == sc->thresh_cap) {
        size_t next = sc->thresh_cap ? sc->thresh_cap * 2 : 8;
        silent_cache_entry *grown = p_alloc(next * sizeof *grown);
        if (grown) {
            for (size_t i = 0; i < sc->thresh_count; i++) grown[i] = sc->thresholds[i];
            p_free(sc->thresholds);
            sc->thresholds = grown, sc->thresh_cap = next;
        }
    }
    if (sc->thresh_count < sc->thresh_cap) {
        silent_cache_entry *e = &sc->thresholds[sc->thresh_count++];
        size_t i = 0;
        for (; i + 1 < sizeof e->project && project[i]; i++) e->project[i] = project[i];
        e->project[i] = 0;
        e->us = us;
        e->failed = (r != P_OK);
    }
    if (r != P_OK) return P_FAILED;
    *out_us = us;
    return P_OK;
}

bool watch_classify_job(store *s, const place *here, const message *m, const job *now, uint64_t clock_now,
                        session_cache *sc, bool *overdue_out, bool *silent_out) {
    *overdue_out = m->due && (now->state == OPEN || now->state == TAKEN) && m->due < clock_now;
    *silent_out = false;
    if (now->state != TAKEN || !sc) return true;
    char proj[36];
    job_project(m->to, proj, sizeof proj);
    uint64_t silent_us = 0;
    if (cached_silent_us(s, here, sc, proj, &silent_us) != P_OK) return false;
    if (!ensure_session_loaded(s, sc, now->by, now->session)) return false;
    *silent_out = is_silent(sc->items, sc->count, now, clock_now, silent_us);
    return true;
}

bool watch_classify_session_silent(store *s, const place *here, const message *m, uint64_t since_us, uint64_t clock_now,
                                   session_cache *sc, bool *silent_out) {
    *silent_out = false;
    if (!m || !m->for_session || !*m->for_session || !sc) return true;
    char proj[36];
    job_project(m->to, proj, sizeof proj);
    uint64_t silent_us = 0;
    if (cached_silent_us(s, here, sc, proj, &silent_us) != P_OK) return false;
    if (!ensure_session_loaded(s, sc, m->to, m->for_session)) return false;
    *silent_out = is_session_silent(sc->items, sc->count, m->to, m->for_session, since_us, clock_now, silent_us);
    return true;
}

static const char *word(const char *at, const char **from, size_t *n) {
    while (*at == ' ') at++;
    *from = at;
    while (*at && *at != ' ' && *at != '\n') at++;
    *n = (size_t)(at - *from);
    return at;
}

/* The same place has one spelling: forward slashes, none twice, no "." steps. */
static void spell(text *out, const char *in) {
    bool start = true;
    for (const char *c = in; *c;) {
        bool slash = *c == '/' || *c == '\\';
        if (start && *c == '.' && (c[1] == '/' || c[1] == '\\')) {
            c += 2;
        } else if (slash && out->length && out->data[out->length - 1] == '/') {
            c++;
        } else {
            add_bytes(out, slash ? "/" : c, 1);
            start = slash || *c == ' ';
            c++;
        }
    }
}

/* One is the other, or a folder above it. A trailing slash changes nothing. */
static bool meet(const char *a, size_t n, const char *b, size_t m) {
    while (n && a[n - 1] == '/') n--;
    while (m && b[m - 1] == '/') m--;
    size_t same_until = n < m ? n : m;
    for (size_t i = 0; i < same_until; i++) if (a[i] != b[i]) return false;
    return n == m || (n < m ? b[n] : a[m]) == '/';
}

static bool overlap(const char *a, const char *b, text *which) {
    const char *from, *other;
    size_t n, m;
    for (const char *at = a; (at = word(at, &from, &n)), n;)
        for (const char *bt = b; (bt = word(bt, &other, &m)), m;)
            if (meet(from, n, other, m)) return add_bytes(which, n > m ? from : other, n > m ? n : m), true;
    return false;
}

typedef struct { store *s; const char *project, *me, *paths; uint64_t now; text *said; s_result result; } look;

static bool other_lane(void *user, const char *name) {
    look *l = user;
    size_t n = len(l->project);
    if (!starts(name, l->project) || name[n] != '+') return true;
    for (const char *c = name; *c; c++) if (*c == '.') return true; /* being written */
    text them = {0}, path = {0}, which = {0}, id = {0}, key = {0}, body = {0};
    add(&them, l->project), add(&them, "/"), add(&them, name + n + 1);
    add(&path, l->s->folder), add(&path, "/working/"), add(&path, name);
    char *data = NULL;
    size_t length;
    p_result got = P_MISSING;
    if (them.failed || path.failed) got = P_FAILED;
    else if (!same(them.data, l->me)) got = p_read(path.data, MOST_PATHS + 64, &data, &length);
    if (got != P_OK && got != P_MISSING) l->s->why = "what another lane is working on cannot be read", l->result = S_DAMAGED;
    if (got == P_OK) {
        uint64_t when = 0;
        const char *at = data;
        for (; *at >= '0' && *at <= '9'; at++) when = when * 10 + (uint64_t)(*at - '0');
        if (*at == '\n' && when + 4ull * 3600 * 1000000 > l->now && overlap(l->paths, at + 1, &which)) {
            add(l->said, them.data), add(l->said, " is also working on "), add(l->said, which.data ? which.data : "?"), add(l->said, "\n");
            add(&key, "overlap "), add_number(&key, l->now / 86400000000ull, 0), add(&key, " ");
            uint64_t hash = 14695981039346656037ull; /* a key is short and a path need not be */
            for (const char *c = which.data ? which.data : ""; *c; c++) hash = (hash ^ (unsigned char)*c) * 1099511628211ull;
            add_number(&key, hash, 20);
            add(&body, l->me), add(&body, " started to work on "), add(&body, which.data ? which.data : "?"), add(&body, ", which you said you are working on.");
            message m = {.from = l->me, .to = them.data, .kind = "headsup", .about = which.data ? which.data : "", .urgency = "now", .re = "",
                         .subject = "We are working on the same files", .body = body.data ? body.data : ""};
            s_result sent = key.failed || body.failed || which.failed ? S_DAMAGED : store_post(l->s, &m, key.data, &id);
            if (sent != S_OK) l->result = sent;
        }
        p_free(data);
    }
    text_free(&them), text_free(&path), text_free(&which), text_free(&id), text_free(&key), text_free(&body);
    return true;
}

s_result watch_working(store *s, const place *p, const char *me, const char *paths, text *said) {
    if (!p->found) return s->why = "there is no helio.conf here, so the paths have no project", S_REFUSED;
    text line = {0}, folder = {0}, path = {0}, data = {0}, unique = {0}, unspelled = {0};
    clean(&unspelled, paths, true);
    spell(&line, unspelled.data ? unspelled.data : "");
    s_result result = S_OK;
    size_t n = len(p->project);
    if (line.length > MOST_PATHS) s->why = "that is too many paths", result = S_REFUSED;
    else if (!starts(me, p->project) || me[n] != '/') s->why = "you are not a lane of this project", result = S_REFUSED;
    else {
        add(&folder, s->folder), add(&folder, "/working");
        add(&path, folder.data ? folder.data : ""), add(&path, "/");
        for (const char *c = me; *c; c++) add_bytes(&path, *c == '/' ? "+" : c, 1);
        add_number(&data, p_now(), 0), add(&data, "\n"), add(&data, line.data ? line.data : ""), add(&data, "\n"), add_number(&unique, p_tick(), 0);
        bool full = unspelled.failed || line.failed || folder.failed || path.failed || data.failed || unique.failed;
        if (full || p_folder(folder.data) != P_OK || p_write(path.data, data.data, data.length, true, unique.data) != P_OK)
            s->why = "the store cannot be written", result = S_DAMAGED;
        else {
            look l = {s, p->project, me, line.data ? line.data : "", p_now(), said, S_OK};
            if (p_list(folder.data, other_lane, &l) != P_OK) s->why = "the store cannot be read", result = S_DAMAGED;
            else result = l.result;
        }
    }
    text_free(&line), text_free(&folder), text_free(&path), text_free(&data), text_free(&unique), text_free(&unspelled);
    return result;
}

static void add_summary_item(text *part, unsigned *count, const message *m, const job *j) {
    (*count)++;
    if (*count <= 100) {
        add(part, "- "), add(part, m->id), add(part, " "), add(part, m->from), add(part, " to "), add(part, m->to);
        if (m->repository && *m->repository) add(part, " (in "), add(part, m->repository), add(part, ")");
        if (*j->by) add(part, ", by "), add(part, j->by);
        add(part, ": "), clean(part, m->subject, true), add(part, "\n");
    }
}

s_result watch_summary(store *s, const place *here) {
    text day = {0}, folder = {0}, path = {0}, unique = {0};
    add_time(&day, p_now());
    if (day.data) day.data[day.length = 10] = 0;
    add(&folder, s->folder), add(&folder, "/summary");
    add(&path, folder.data ? folder.data : ""), add(&path, "/"), add(&path, day.data ? day.data : ""), add(&path, ".md");
    add_number(&unique, p_tick(), 0);
    bool full = day.failed || folder.failed || path.failed || unique.failed;
    char *there = NULL;
    size_t length;
    s_result result = S_OK;
    p_result got = full ? P_FAILED : p_read(path.data, 1 << 20, &there, &length);
    if (got != P_OK && got != P_MISSING) result = S_DAMAGED;
    else if (got == P_MISSING) {
        static const char *const titles[] = {"Overdue", "Silent", "Open", "Taken", "Done in the last day", "Refused in the last day"};
        text parts[6] = {0}, out = {0};
        unsigned counts[6] = {0}, damaged = 0;
        uint64_t now = p_now();
        session_cache sc = {0};
        bool liveness_err = false;
        for (size_t i = 0; i < s->count; i++) {
            size_t n = len(s->names[i]);
            if (n <= 5 || !same(s->names[i] + n - 5, ".post")) continue;
            char *id = copy(s->names[i], n - 5);
            message m = {0};
            job j;
            s_result mr = id ? store_message_live(s, id, &m) : S_DAMAGED;
            if (mr == S_REFUSED && same(s->why, "there is no such message")) {
                p_free(id);
                continue;
            }
            s_result jr = mr == S_OK && is_job(&m) ? store_job(s, &m, &j) : mr;
            if (mr == S_OK && jr == S_REFUSED) {
                message_free(&m);
                p_free(id);
                continue;
            }
            if (jr != S_OK) damaged++;
            else if (is_job(&m)) {
                bool ov = false, si = false;
                if (!watch_classify_job(s, here, &m, &j, now, &sc, &ov, &si))
                    liveness_err = true;
                if (ov) add_summary_item(&parts[0], &counts[0], &m, &j);
                if (si) add_summary_item(&parts[1], &counts[1], &m, &j);
                if (!ov && !si) {
                    if (j.state == OPEN) add_summary_item(&parts[2], &counts[2], &m, &j);
                    else if (j.state == TAKEN) add_summary_item(&parts[3], &counts[3], &m, &j);
                    else if (j.when + 86400000000ull > now) {
                        int part = j.state == DONE ? 4 : 5;
                        add_summary_item(&parts[part], &counts[part], &m, &j);
                    }
                }
            }
            message_free(&m);
            p_free(id);
        }
        sessions_cache_free(&sc);
        if (liveness_err) {
            /* Do not freeze an incomplete daily summary when session or threshold reads failed:
               return S_NOTHING (deferred) so the caller prints a diagnostic, retries on the next
               call once the transient fault clears, and lets the caller's own inbox decide its exit code. */
            for (int i = 0; i < 6; i++) text_free(&parts[i]);
            s->why = "the summary was deferred: session or threshold could not be read";
            p_free(there), text_free(&day), text_free(&folder), text_free(&path), text_free(&unique);
            return S_NOTHING;
        } else {
            add(&out, "# Heliograph "), add(&out, day.data), add(&out, "\n\nAs it stood at "), add_time(&out, now), add(&out, ".\n");
            for (int i = 0; i < 6; i++) {
                add(&out, "\n## "), add(&out, titles[i]), add(&out, ": "), add_number(&out, counts[i], 0), add(&out, "\n\n");
                if (parts[i].data) add(&out, parts[i].data);
                if (counts[i] > 100) add(&out, "- and more: helio log\n");
                if (parts[i].failed) out.failed = true;
                text_free(&parts[i]);
            }
            if (damaged) add(&out, "\nDamaged: "), add_number(&out, damaged, 0), add(&out, ". See helio check.\n");
            p_result wrote = out.failed || p_folder(folder.data) != P_OK ? P_FAILED : p_write(path.data, out.data, out.length, false, unique.data);
            if (wrote != P_OK && wrote != P_EXISTS) result = S_DAMAGED;
            text_free(&out);
        }
    }
    if (result != S_OK) s->why = "the summary cannot be written";
    p_free(there), text_free(&day), text_free(&folder), text_free(&path), text_free(&unique);
    return result;
}
