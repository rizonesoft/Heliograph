#include "place.h"

enum { MOST_SESSION_FILE = 512 };

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
        if (starts(line, "project:")) value += 8, good = take_word(p->project, sizeof p->project, &value);
        else if (starts(line, "lane:")) value += 5, good = take_word(p->lane, sizeof p->lane, &value);
        else if (starts(line, "repository:")) value += 11, good = take_word(p->repository, sizeof p->repository, &value) && pair_ok("x", p->repository);
        else if (starts(line, "path:")) value += 5, good = take_word(path, sizeof path, &value) && take_word(lane, sizeof lane, &value) && pair_ok("x", lane);
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

static void hello_path(const store *s, text *out, const char *lane, const char *session) {
    add(out, s->folder), add(out, "/sessions/");
    for (; *lane; lane++) add_bytes(out, *lane == '/' ? "+" : lane, 1);
    if (session && *session) add(out, "+"), add_hex(out, session);
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

static void merge_meta(session_info *info, const session_info *meta, bool has_meta) {
    if (!has_meta || info->legacy) return;
    if (meta->when > info->when) {
        info->when = meta->when;
        if (*meta->agent) copy_str(info->agent, sizeof info->agent, meta->agent);
        copy_str(info->repository, sizeof info->repository, meta->repository);
    } else if (!*info->agent && *meta->agent) {
        copy_str(info->agent, sizeof info->agent, meta->agent);
        if (meta->when >= info->when && !*info->repository && *meta->repository)
            copy_str(info->repository, sizeof info->repository, meta->repository);
    }
}

s_result place_hello(store *s, const char *lane, const char *agent, const char *repository, const char *session) {
    if (!lane || !address_ok(lane, false)) return s->why = "say who you are: --as project/lane or HELIO_AS", S_REFUSED;
    if (!session_ok(session)) return s->why = "the session name is invalid or too long", S_REFUSED;
    if (repository && *repository && len(repository) > 32) return s->why = "the repository name is too long", S_REFUSED;
    const char *ag = agent ? agent : "";
    const char *rp = repository ? repository : "";
    const char *ss = session ? session : "";
    uint64_t now = p_now();

    text folder = {0}, path = {0}, meta_path = {0}, data = {0}, unique = {0};
    add(&folder, s->folder), add(&folder, "/sessions");
    hello_path(s, &path, lane, ss);
    add(&meta_path, path.data ? path.data : ""), add(&meta_path, ".meta");
    add_number(&unique, p_tick(), 0);
    if (folder.failed || path.failed || meta_path.failed || unique.failed) {
        text_free(&folder), text_free(&path), text_free(&meta_path), text_free(&unique);
        return s->why = "out of memory", S_DAMAGED;
    }

    p_result result = p_folder(folder.data);
    if (result != P_OK) {
        text_free(&folder), text_free(&path), text_free(&meta_path), text_free(&unique);
        return s->why = "the session cannot be announced", S_DAMAGED;
    }

    if (*ag) {
        format_session(&data, now, ag, rp, ss);
        if (data.failed) result = P_FAILED;
        else {
            result = p_write(meta_path.data, data.data, data.length, true, unique.data);
            if (result == P_OK) result = p_write(path.data, data.data, data.length, true, unique.data);
        }
    } else {
        session_info existing = {0}, meta = {0};
        bool have_existing = false, have_meta = false;
        p_result er = read_session_file(path.data, &existing, &have_existing);
        if (er != P_OK && er != P_MISSING) {
            text_free(&folder), text_free(&path), text_free(&meta_path), text_free(&unique);
            return s->why = "the existing session cannot be read", S_DAMAGED;
        }
        p_result mr = read_session_file(meta_path.data, &meta, &have_meta);
        if (mr != P_OK && mr != P_MISSING) {
            text_free(&folder), text_free(&path), text_free(&meta_path), text_free(&unique);
            return s->why = "the existing session metadata cannot be read", S_DAMAGED;
        }
        const char *use_agent = (have_meta && *meta.agent) ? meta.agent : (have_existing && *existing.agent) ? existing.agent : "";
        const char *use_repo = rp;
        if (s->pause) s->pause(s->pause_user, "hello-write");

        format_session(&data, now, use_agent, use_repo, ss);
        if (data.failed) result = P_FAILED;
        else if (er == P_MISSING) {
            result = p_write(path.data, data.data, data.length, false, unique.data);
            if (result == P_EXISTS) {
                /* A concurrent hello or refresh created the file while we were preparing. */
                session_info raced = {0}, raced_meta = {0};
                bool have_raced = false, have_raced_meta = false;
                p_result rr = read_session_file(path.data, &raced, &have_raced);
                p_result rmr = read_session_file(meta_path.data, &raced_meta, &have_raced_meta);
                if ((rr != P_OK && rr != P_MISSING) || (rmr != P_OK && rmr != P_MISSING)) {
                    result = P_FAILED;
                } else {
                    const char *ra = (have_raced_meta && *raced_meta.agent) ? raced_meta.agent : (have_raced && *raced.agent) ? raced.agent : use_agent;
                    const char *rrp = use_repo;
                    uint64_t rwhen = now;
                    if (have_raced_meta && raced_meta.when >= rwhen) {
                        rwhen = raced_meta.when;
                        if (!*rrp && *raced_meta.repository) rrp = raced_meta.repository;
                    }
                    if (have_raced && raced.when > rwhen) rwhen = raced.when;
                    text_free(&data);
                    format_session(&data, rwhen, ra, rrp, ss);
                    result = data.failed ? P_FAILED : p_write(path.data, data.data, data.length, true, unique.data);
                }
            }
        } else {
            result = p_write(path.data, data.data, data.length, true, unique.data);
        }
    }

    text_free(&folder), text_free(&path), text_free(&meta_path), text_free(&data), text_free(&unique);
    if (result != P_OK) s->why = "the session cannot be announced";
    return result == P_OK ? S_OK : S_DAMAGED;
}

s_result place_session(store *s, const char *lane, const char *session, session_info *out) {
    *out = (session_info){0};
    if (!lane || !address_ok(lane, false) || !session_ok(session)) return s->why = "invalid lane or session", S_REFUSED;
    text path = {0}, meta_path = {0};
    hello_path(s, &path, lane, session ? session : "");
    add(&meta_path, path.data ? path.data : ""), add(&meta_path, ".meta");
    if (path.failed || meta_path.failed) {
        text_free(&path), text_free(&meta_path);
        return s->why = "out of memory", S_DAMAGED;
    }
    bool parsed = false, has_meta = false;
    session_info meta = {0};
    p_result r = read_session_file(path.data, out, &parsed);
    if (r == P_MISSING) {
        text_free(&path), text_free(&meta_path);
        return S_NOTHING;
    }
    if (r != P_OK || !parsed) {
        text_free(&path), text_free(&meta_path);
        return s->why = "the session cannot be read", S_DAMAGED;
    }
    p_result mr = read_session_file(meta_path.data, &meta, &has_meta);
    text_free(&path), text_free(&meta_path);
    if (mr == P_OK && has_meta) merge_meta(out, &meta, true);
    copy_str(out->lane, sizeof out->lane, lane);
    return S_OK;
}

typedef struct {
    store *s;
    bool (*each)(void *user, const session_info *info);
    void *user;
} session_scan;

static bool decode_lane_from_name(const char *name, char *lane, size_t room) {
    size_t plus1 = 0;
    while (name[plus1] && name[plus1] != '+') plus1++;
    if (!name[plus1]) return false;
    size_t plus2 = plus1 + 1;
    while (name[plus2] && name[plus2] != '+') plus2++;
    if (plus2 == plus1 + 1 || plus2 >= room) return false;
    for (size_t i = 0; i < plus2; i++) lane[i] = i == plus1 ? '/' : name[i];
    lane[plus2] = 0;
    return address_ok(lane, false);
}

static bool scan_one_session(void *user, const char *name) {
    session_scan *sc = user;
    size_t n = len(name);
    if (n > 5 && same(name + n - 5, ".part")) return true;
    if (n > 5 && same(name + n - 5, ".meta")) return true;
    char lane[72];
    if (!decode_lane_from_name(name, lane, sizeof lane)) return true;
    text path = {0}, meta_path = {0};
    add(&path, sc->s->folder), add(&path, "/sessions/"), add(&path, name);
    add(&meta_path, path.data ? path.data : ""), add(&meta_path, ".meta");
    bool keep = true;
    if (!path.failed && !meta_path.failed) {
        session_info info = {0}, meta = {0};
        bool parsed = false, has_meta = false;
        if (read_session_file(path.data, &info, &parsed) == P_OK && parsed) {
            if (read_session_file(meta_path.data, &meta, &has_meta) == P_OK && has_meta) merge_meta(&info, &meta, true);
            copy_str(info.lane, sizeof info.lane, lane);
            keep = sc->each(sc->user, &info);
        }
    }
    text_free(&path), text_free(&meta_path);
    return keep;
}

p_result place_each_session(store *s, bool (*each)(void *user, const session_info *info), void *user) {
    text folder = {0};
    add(&folder, s->folder), add(&folder, "/sessions");
    if (folder.failed) {
        text_free(&folder);
        return P_FAILED;
    }
    session_scan sc = {s, each, user};
    p_result r = p_list(folder.data, scan_one_session, &sc);
    text_free(&folder);
    return r;
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

bool place_busy(store *s, const char *project, const char *me, uint64_t within) {
    uint64_t now = p_now();
    look l = {project, me, now > within ? now - within : 0, false};
    place_each_session(s, busy_each, &l);
    return l.busy;
}
