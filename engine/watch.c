#include "watch.h"

enum { MOST_PATHS = 2000 };

bool is_overdue(const message *m, const job *now) {
    return m->due && (now->state == OPEN || now->state == TAKEN) && m->due < p_now();
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

s_result watch_summary(store *s) {
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
        static const char *const titles[] = {"Overdue", "Open", "Taken", "Done in the last day", "Refused in the last day"};
        text parts[5] = {0}, out = {0};
        unsigned counts[5] = {0}, damaged = 0;
        uint64_t now = p_now();
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
                int part = is_overdue(&m, &j) ? 0 : j.state == OPEN ? 1 : j.state == TAKEN ? 2 : j.state == DONE ? 3 : 4;
                if (part < 3 || j.when + 86400000000ull > now) {
                    counts[part]++;
                    if (counts[part] <= 100) {
                        add(&parts[part], "- "), add(&parts[part], m.id), add(&parts[part], " "), add(&parts[part], m.from), add(&parts[part], " to "), add(&parts[part], m.to);
                        if (*j.by) add(&parts[part], ", by "), add(&parts[part], j.by);
                        add(&parts[part], ": "), clean(&parts[part], m.subject, true), add(&parts[part], "\n");
                    }
                }
            }
            message_free(&m);
            p_free(id);
        }
        add(&out, "# Heliograph "), add(&out, day.data), add(&out, "\n\nAs it stood at "), add_time(&out, now), add(&out, ".\n");
        for (int i = 0; i < 5; i++) {
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
    if (result != S_OK) s->why = "the summary cannot be written";
    p_free(there), text_free(&day), text_free(&folder), text_free(&path), text_free(&unique);
    return result;
}
