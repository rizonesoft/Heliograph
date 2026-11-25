#include "place.h"

enum { MOST_SESSION_FILE = 512, MOST_CAS_ATTEMPTS = 64 };

static bool take_word(char *to, size_t room, const char **at) {
    while (**at == ' ') ++*at;
    size_t n = 0;
    while (**at && **at != ' ' && **at != '\n' && **at != '\r') {
        if (n + 1 >= room) return false;
        to[n++] = *(*at)++;
    }
    to[n] = 0;
    return n > 0;
}

static bool line_end(const char *at) {
    while (*at == ' ' || *at == '\r') at++;
    return !*at || *at == '\n';
}

static const char *next_line(const char *at) {
    while (*at && *at != '\n') at++;
    return *at ? at + 1 : at;
}

static bool pair_ok(const char *project, const char *lane) {
    text whole = {0};
    add(&whole, project), add(&whole, "/"), add(&whole, lane);
    bool good = !whole.failed && address_ok(whole.data, false);
    text_free(&whole);
    return good;
}

static void copy_str(char *to, size_t room, const char *from) {
    if (!room) return;
    size_t i = 0;
    if (from)
        while (i + 1 < room && from[i]) to[i] = from[i], i++;
    to[i] = 0;
}

s_result place_open(place *p, const char **why) {
    *p = (place){0};
    text at = {0};
    for (int up = 0; up < 24 && !p->file; up++) {
        text file = {0};
        add(&file, at.data ? at.data : ""), add(&file, "helio.conf");
        size_t length;
        p_result result = file.failed ? P_FAILED : p_read(file.data, 65536, &p->file, &length);
        text_free(&file);
        if (result != P_OK && result != P_MISSING) {
            text_free(&at);
            return *why = "helio.conf cannot be read", S_DAMAGED;
        }
        add(&at, "../");
    }
    text_free(&at);
    if (!p->file) return S_OK;
    for (const char *line = p->file; *line; line = next_line(line)) {
        const char *value = line;
        bool good = true;
        char path[512], lane[36];
        if (starts(line, "project:")) value += 8, good = take_word(p->project, sizeof p->project, &value) && line_end(value);
        else if (starts(line, "lane:")) value += 5, good = take_word(p->lane, sizeof p->lane, &value) && line_end(value);
        else if (starts(line, "repository:")) value += 11, good = take_word(p->repository, sizeof p->repository, &value) && pair_ok("x", p->repository) && line_end(value);
        else if (starts(line, "path:")) value += 5, good = take_word(path, sizeof path, &value) && take_word(lane, sizeof lane, &value) && pair_ok("x", lane) && line_end(value);
        else if (*line != '\n' && *line != '\r' && *line != '#') good = false;
        if (!good) return *why = "helio.conf has a line that is not project, lane, repository or path", S_DAMAGED;
    }
    if (!pair_ok(p->project, *p->lane ? p->lane : "x")) return *why = "helio.conf names no project, or a bad lane", S_DAMAGED;
    p->found = true;
    return S_OK;
}

void place_close(place *p) {
    p_free(p->file);
    *p = (place){0};
}

void place_owner(const place *p, const char *path, text *to) {
    size_t best = 0;
    char winner[36] = "";
    if (starts(path, "./")) path += 2;
    for (const char *line = p->found ? p->file : ""; *line; line = next_line(line)) {
        if (!starts(line, "path:")) continue;
        char owned[512], lane[36];
        const char *value = line + 5;
        if (!take_word(owned, sizeof owned, &value) || !take_word(lane, sizeof lane, &value)) continue;
        size_t n = len(owned);
        bool under = starts(path, owned) && (owned[n - 1] == '/' || !path[n] || path[n] == '/');
        if (under && n > best) {
            best = n;
            copy_str(winner, sizeof winner, lane);
        }
    }
    if (*winner) add(to, p->project), add(to, "/"), add(to, winner);
}

static void add_hex(text *out, const char *s) {
    static const char hex[] = "0123456789abcdef";
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        char pair[2] = {hex[*p >> 4], hex[*p & 15]};
        add_bytes(out, pair, 2);
    }
}

static void session_base(text *out, const char *lane, const char *session) {
    for (; *lane; lane++) add_bytes(out, *lane == '/' ? "+" : lane, 1);
    if (session && *session) add(out, "+"), add_hex(out, session);
}

static void session_seq_path(const store *s, text *out, const char *base, uint64_t seq) {
    add(out, s->folder), add(out, "/sessions/"), add(out, base);
    if (seq > 0) add(out, "~"), add_number(out, seq, 0);
}

static bool parse_u64(const char *s, size_t n, uint64_t *out) {
    if (!n) return false;
    uint64_t v = 0;
    for (size_t i = 0; i < n; i++) {
        if (s[i] < '0' || s[i] > '9') return false;
        uint64_t d = (uint64_t)(s[i] - '0');
        if (v > (UINT64_MAX - d) / 10) return false;
        v = v * 10 + d;
    }
    *out = v;
    return true;
}

static void copy_slice(char *to, size_t room, const char *from, size_t n) {
    if (!room) return;
    size_t take = n < room - 1 ? n : room - 1;
    for (size_t i = 0; i < take; i++) to[i] = from[i];
    to[take] = 0;
}

bool place_parse_session(const char *data, size_t length, session_info *out) {
    *out = (session_info){0};
    if (!data || !length) return false;
    if (data[0] >= '0' && data[0] <= '9') {
        size_t i = 0;
        while (i < length && data[i] >= '0' && data[i] <= '9') i++;
        if (i < length && data[i] != '\n' && data[i] != '\r') return false;
        if (!parse_u64(data, i, &out->when)) return false;
        out->legacy = true;
        return true;
    }
    bool have_time = false;
    size_t pos = 0;
    while (pos < length) {
        size_t end = pos;
        while (end < length && data[end] != '\n') end++;
        size_t line_len = end - pos;
        if (line_len && data[pos + line_len - 1] == '\r') line_len--;
        const char *line = data + pos;
        if (line_len >= 6 && starts(line, "time: ")) {
            if (!parse_u64(line + 6, line_len - 6, &out->when)) return false;
            have_time = true;
        } else if (line_len >= 7 && starts(line, "agent: ")) {
            copy_slice(out->agent, sizeof out->agent, line + 7, line_len - 7);
        } else if (line_len >= 12 && starts(line, "repository: ")) {
            copy_slice(out->repository, sizeof out->repository, line + 12, line_len - 12);
        } else if (line_len >= 9 && starts(line, "session: ")) {
            copy_slice(out->session, sizeof out->session, line + 9, line_len - 9);
        }
        pos = end < length ? end + 1 : length;
    }
    return have_time;
}

static void format_session(text *out, uint64_t when, const char *agent, const char *repository, const char *session) {
    add(out, "time: "), add_number(out, when, 0), add(out, "\n");
    add(out, "agent: "), add(out, agent ? agent : ""), add(out, "\n");
    add(out, "repository: "), add(out, repository ? repository : ""), add(out, "\n");
    add(out, "session: "), add(out, session ? session : ""), add(out, "\n");
}

static p_result read_session_file(const char *path, session_info *out, bool *parsed) {
    char *data = NULL;
    size_t length = 0;
    p_result r = p_read(path, MOST_SESSION_FILE, &data, &length);
    if (r == P_OK) {
        *parsed = place_parse_session(data, length, out);
        p_free(data);
    } else {
        *parsed = false;
        *out = (session_info){0};
    }
    return r;
}

/* Validates `<project>+<lane>[+<even-hex>][~<seq>]`. Sets base_len, lane, and seq (0 if no ~<seq>). */
static bool parse_session_filename(const char *name, size_t *base_len, char *lane, size_t lane_room, uint64_t *seq) {
    size_t n = len(name);
    if (n > 5 && same(name + n - 5, ".part")) return false;
    size_t tilde = 0;
    while (name[tilde] && name[tilde] != '~') tilde++;
    uint64_t s = 0;
    if (name[tilde] == '~') {
        const char *digits = name + tilde + 1;
        size_t dlen = n - tilde - 1;
        if (!dlen || digits[0] == '0' || !parse_u64(digits, dlen, &s) || s == 0) return false;
    }
    size_t blen = tilde;
    size_t plus1 = 0;
    while (plus1 < blen && name[plus1] != '+') plus1++;
    if (plus1 >= blen) return false;
    size_t plus2 = plus1 + 1;
    while (plus2 < blen && name[plus2] != '+') plus2++;
    if (plus2 == plus1 + 1 || plus2 >= lane_room) return false;
    for (size_t i = 0; i < plus2; i++) lane[i] = i == plus1 ? '/' : name[i];
    lane[plus2] = 0;
    if (!address_ok(lane, false)) return false;
    if (plus2 < blen) {
        size_t hlen = blen - plus2 - 1;
        if (hlen < 2 || hlen > 128 || (hlen & 1u)) return false;
        for (size_t i = plus2 + 1; i < blen; i++) {
            char c = name[i];
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
        }
    }
    *base_len = blen;
    *seq = s;
    return true;
}

typedef struct {
    const char *base;
    size_t base_len;
    bool found;
    uint64_t max_seq;
} latest_scan;

static bool find_latest_cb(void *user, const char *name) {
    latest_scan *ls = user;
    size_t blen = 0;
    char lane[72];
    uint64_t seq = 0;
    if (!parse_session_filename(name, &blen, lane, sizeof lane, &seq)) return true;
    if (blen != ls->base_len) return true;
    for (size_t i = 0; i < blen; i++)
        if (name[i] != ls->base[i]) return true;
    if (!ls->found || seq > ls->max_seq) ls->found = true, ls->max_seq = seq;
    return true;
}

static p_result find_latest_seq(const store *s, const char *base, bool *found, uint64_t *max_seq) {
    *found = false, *max_seq = 0;
    text folder = {0};
    add(&folder, s->folder), add(&folder, "/sessions");
    if (folder.failed) {
        text_free(&folder);
        return P_FAILED;
    }
    latest_scan ls = {base, len(base), false, 0};
    p_result r = p_list(folder.data, find_latest_cb, &ls);
    text_free(&folder);
    if (r == P_OK) *found = ls.found, *max_seq = ls.max_seq;
    return r;
}

/* Lock-free monotonic CAS: each update atomically creates `<base>` (seq 0) or
   `<base>~<seq+1>` via p_create_excl. On P_EXISTS another writer won that slot,
   so we re-read the latest generation, merge monotonically, and retry. After
   publishing, the superseded generation is removed best-effort. */
s_result place_hello(store *s, const char *lane, const char *agent, const char *repository, const char *session) {
    if (!lane || !address_ok(lane, false)) return s->why = "say who you are: --as project/lane or HELIO_AS", S_REFUSED;
    if (!session_ok(session)) return s->why = "the session name is invalid or too long", S_REFUSED;
    if (repository && *repository && len(repository) > 32) return s->why = "the repository name is too long", S_REFUSED;
    const char *ag = agent ? agent : "";
    const char *rp = repository ? repository : "";
    const char *ss = session ? session : "";

    text folder = {0}, base = {0};
    add(&folder, s->folder), add(&folder, "/sessions");
    session_base(&base, lane, ss);
    if (folder.failed || base.failed) {
        text_free(&folder), text_free(&base);
        return s->why = "out of memory", S_DAMAGED;
    }
    if (p_folder(folder.data) != P_OK) {
        text_free(&folder), text_free(&base);
        return s->why = "the session cannot be announced", S_DAMAGED;
    }
    text_free(&folder);

    if (s->pause) s->pause(s->pause_user, "hello-write");

    for (int attempt = 0; attempt < MOST_CAS_ATTEMPTS; attempt++) {
        bool have_prev = false;
        uint64_t prev_seq = 0;
        p_result lr = find_latest_seq(s, base.data, &have_prev, &prev_seq);
        if (lr != P_OK) {
            text_free(&base);
            return s->why = "the session cannot be announced", S_DAMAGED;
        }

        session_info existing = {0};
        bool have_existing = false;
        text prev_path = {0};
        if (have_prev) {
            session_seq_path(s, &prev_path, base.data, prev_seq);
            if (prev_path.failed) {
                text_free(&prev_path), text_free(&base);
                return s->why = "out of memory", S_DAMAGED;
            }
            p_result er = read_session_file(prev_path.data, &existing, &have_existing);
            if (er == P_MISSING) {
                text_free(&prev_path);
                continue; /* Concurrent writer published prev_seq+1 and swept prev_seq. */
            }
            if (er != P_OK) {
                text_free(&prev_path), text_free(&base);
                return s->why = "the existing session cannot be read", S_DAMAGED;
            }
        }

        uint64_t next_seq = have_prev ? prev_seq + 1 : 0;
        uint64_t now = p_now();
        if (have_existing && now <= existing.when) now = existing.when + 1;
        const char *use_agent = *ag ? ag : (have_existing && *existing.agent ? existing.agent : "");

        text next_path = {0}, data = {0};
        session_seq_path(s, &next_path, base.data, next_seq);
        format_session(&data, now, use_agent, rp, ss);
        if (next_path.failed || data.failed) {
            text_free(&next_path), text_free(&data), text_free(&prev_path), text_free(&base);
            return s->why = "out of memory", S_DAMAGED;
        }

        if (s->pause) s->pause(s->pause_user, "hello-create");

        p_result cr = p_create_excl(next_path.data, data.data, data.length);
        text_free(&next_path), text_free(&data);
        if (cr == P_EXISTS) {
            text_free(&prev_path);
            if (s->pause) s->pause(s->pause_user, "hello-retry");
            continue;
        }
        if (cr == P_OK || cr == P_UNFLUSHED) {
            if (have_prev && prev_path.data) p_remove(prev_path.data);
            text_free(&prev_path), text_free(&base);
            if (cr == P_UNFLUSHED) return s->why = "the session was written, but its folder could not be flushed", S_DAMAGED;
            return S_OK;
        }
        text_free(&prev_path), text_free(&base);
        return s->why = "the session cannot be announced", S_DAMAGED;
    }

    text_free(&base);
    return s->why = "the session cannot be announced", S_DAMAGED;
}

s_result place_session(store *s, const char *lane, const char *session, session_info *out) {
    *out = (session_info){0};
    if (!lane || !address_ok(lane, false) || !session_ok(session)) return s->why = "invalid lane or session", S_REFUSED;
    text base = {0};
    session_base(&base, lane, session ? session : "");
    if (base.failed) {
        text_free(&base);
        return s->why = "out of memory", S_DAMAGED;
    }
    for (int attempt = 0; attempt < 8; attempt++) {
        bool found = false;
        uint64_t max_seq = 0;
        p_result lr = find_latest_seq(s, base.data, &found, &max_seq);
        if (lr == P_MISSING || (lr == P_OK && !found)) {
            text_free(&base);
            return S_NOTHING;
        }
        if (lr != P_OK) {
            text_free(&base);
            return s->why = "the session cannot be read", S_DAMAGED;
        }
        text path = {0};
        session_seq_path(s, &path, base.data, max_seq);
        if (path.failed) {
            text_free(&path), text_free(&base);
            return s->why = "out of memory", S_DAMAGED;
        }
        bool parsed = false;
        p_result r = read_session_file(path.data, out, &parsed);
        text_free(&path);
        if (r == P_MISSING) continue; /* Concurrent writer swept max_seq after publishing max_seq+1. */
        text_free(&base);
        if (r != P_OK || !parsed) return s->why = "the session cannot be read", S_DAMAGED;
        copy_str(out->lane, sizeof out->lane, lane);
        return S_OK;
    }
    text_free(&base);
    return s->why = "the session cannot be read", S_DAMAGED;
}

typedef struct {
    char *base;
    char lane[72];
    uint64_t seq;
} latest_entry;

typedef struct {
    latest_entry *items;
    size_t count, cap;
    bool failed;
} latest_map;

static bool collect_latest_cb(void *user, const char *name) {
    latest_map *m = user;
    size_t blen = 0;
    char lane[72];
    uint64_t seq = 0;
    if (!parse_session_filename(name, &blen, lane, sizeof lane, &seq)) return true;
    for (size_t i = 0; i < m->count; i++) {
        if (len(m->items[i].base) == blen) {
            bool eq = true;
            for (size_t j = 0; j < blen && eq; j++) eq = m->items[i].base[j] == name[j];
            if (eq) {
                if (seq > m->items[i].seq) m->items[i].seq = seq;
                return true;
            }
        }
    }
    if (m->count == m->cap) {
        size_t next = m->cap ? m->cap * 2 : 32;
        latest_entry *grown = p_alloc(next * sizeof *grown);
        if (!grown) { m->failed = true; return false; }
        for (size_t i = 0; i < m->count; i++) grown[i] = m->items[i];
        p_free(m->items);
        m->items = grown, m->cap = next;
    }
    char *bcopy = copy(name, blen);
    if (!bcopy) { m->failed = true; return false; }
    m->items[m->count].base = bcopy;
    copy_str(m->items[m->count].lane, sizeof m->items[m->count].lane, lane);
    m->items[m->count].seq = seq;
    m->count++;
    return true;
}

p_result place_each_session(store *s, bool (*each)(void *user, const session_info *info), void *user) {
    text folder = {0};
    add(&folder, s->folder), add(&folder, "/sessions");
    if (folder.failed) {
        text_free(&folder);
        return P_FAILED;
    }
    latest_map map = {0};
    p_result r = p_list(folder.data, collect_latest_cb, &map);
    text_free(&folder);
    if (r != P_OK || map.failed) {
        for (size_t i = 0; i < map.count; i++) p_free(map.items[i].base);
        p_free(map.items);
        return r != P_OK ? r : P_FAILED;
    }
    bool had_error = false, keep = true;
    for (size_t i = 0; i < map.count && keep; i++) {
        session_info info = {0};
        bool ok = false;
        for (int attempt = 0; attempt < 8; attempt++) {
            uint64_t seq = map.items[i].seq;
            if (attempt > 0) {
                bool found = false;
                if (find_latest_seq(s, map.items[i].base, &found, &seq) != P_OK || !found) break;
            }
            text path = {0};
            session_seq_path(s, &path, map.items[i].base, seq);
            if (path.failed) {
                text_free(&path);
                had_error = true;
                break;
            }
            bool parsed = false;
            p_result rr = read_session_file(path.data, &info, &parsed);
            text_free(&path);
            if (rr == P_MISSING) continue;
            if (rr == P_OK && parsed) {
                copy_str(info.lane, sizeof info.lane, map.items[i].lane);
                ok = true;
            } else {
                had_error = true;
            }
            break;
        }
        if (ok) keep = each(user, &info);
    }
    for (size_t i = 0; i < map.count; i++) p_free(map.items[i].base);
    p_free(map.items);
    return had_error ? P_FAILED : P_OK;
}

typedef struct { const char *project, *me; uint64_t since; bool busy; } look;

static bool busy_each(void *user, const session_info *info) {
    look *l = user;
    size_t n = len(l->project);
    if (!starts(info->lane, l->project) || info->lane[n] != '/') return true;
    if (same(info->lane, l->me)) return true;
    if (info->when >= l->since) l->busy = true;
    return !l->busy;
}

s_result place_busy(store *s, const char *project, const char *me, uint64_t within, bool *busy) {
    *busy = false;
    uint64_t now = p_now();
    look l = {project, me, now > within ? now - within : 0, false};
    p_result r = place_each_session(s, busy_each, &l);
    if (r == P_MISSING) return S_OK;
    if (r != P_OK) return s->why = "the sessions folder cannot be read", S_DAMAGED;
    *busy = l.busy;
    return S_OK;
}
