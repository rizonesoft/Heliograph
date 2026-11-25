#include "place.h"

enum { MOST_SESSION_FILE = 2048, MOST_CAS_ATTEMPTS = 64 };

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

static const uint64_t MAX_SILENT_MINUTES = (~(uint64_t)0) / 60000000ull;

static bool parse_silent_minutes(const char *word, uint64_t *out) {
    if (!word || !*word) return false;
    uint64_t v = 0;
    for (const char *c = word; *c; c++) {
        if (*c < '0' || *c > '9') return false;
        uint64_t d = (uint64_t)(*c - '0');
        if (v > MAX_SILENT_MINUTES / 10 || (v == MAX_SILENT_MINUTES / 10 && d > MAX_SILENT_MINUTES % 10)) return false;
        v = v * 10 + d;
    }
    if (v == 0) return false;
    *out = v;
    return true;
}

static void format_line_error(char *buf, size_t room, size_t lineno, const char *detail) {
    text t = {0};
    add(&t, "helio.conf line "), add_number(&t, (uint64_t)lineno, 0), add(&t, ": "), add(&t, detail);
    copy_str(buf, room, t.failed ? detail : t.data);
    text_free(&t);
}

static bool is_win_path(const char *path) {
    if (!path || !*path) return false;
    if (path[0] == '\\') return true;
    if (((path[0] >= 'A' && path[0] <= 'Z') || (path[0] >= 'a' && path[0] <= 'z')) && path[1] == ':') return true;
    return false;
}

static bool is_sep_for(char c, bool win) { return win ? (c == '\\' || c == '/') : (c == '/'); }

static bool ascii_eq_ci(char a, char b) {
    if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
    if (b >= 'A' && b <= 'Z') b = (char)(b - 'A' + 'a');
    return a == b;
}

size_t place_path_root_len(const char *path, bool *win_out) {
    bool win = is_win_path(path);
    if (win_out) *win_out = win;
    if (!path || !*path) return 0;
    if (!win) return path[0] == '/' ? 1 : 0;
    if (((path[0] >= 'A' && path[0] <= 'Z') || (path[0] >= 'a' && path[0] <= 'z')) && path[1] == ':')
        return is_sep_for(path[2], true) ? 3 : 2;
    if (path[0] == '\\' && path[1] == '\\' && (path[2] == '?' || path[2] == '.') && path[3] == '\\') {
        if (ascii_eq_ci(path[4], 'u') && ascii_eq_ci(path[5], 'n') && ascii_eq_ci(path[6], 'c') && path[7] == '\\') {
            size_t i = 8;
            while (path[i] && path[i] != '\\') i++;
            if (path[i] == '\\') {
                i++;
                while (path[i] && path[i] != '\\') i++;
            }
            return i;
        }
        if (((path[4] >= 'A' && path[4] <= 'Z') || (path[4] >= 'a' && path[4] <= 'z')) && path[5] == ':')
            return path[6] == '\\' ? 7 : 6;
        size_t i = 4;
        while (path[i] && path[i] != '\\') i++;
        return path[i] == '\\' ? i + 1 : i;
    }
    if (path[0] == '\\' && path[1] == '\\') {
        size_t i = 2;
        while (path[i] && !is_sep_for(path[i], true)) i++;
        if (is_sep_for(path[i], true)) {
            i++;
            while (path[i] && !is_sep_for(path[i], true)) i++;
        }
        return i;
    }
    return 1;
}

s_result place_open(place *p, const char **why) {
    *p = (place){0};
    p->silent_minutes = PLACE_SILENT_DEFAULT;
    char *cwd = NULL;
    if (p_cwd(&cwd) != P_OK || !cwd || !*cwd) {
        p_free(cwd);
        return *why = "helio.conf cannot be read", S_DAMAGED;
    }
    bool win = false;
    size_t root = place_path_root_len(cwd, &win);
    if (root == 0) {
        p_free(cwd);
        return *why = "helio.conf cannot be read", S_DAMAGED;
    }
    size_t dir_len = len(cwd);
    while (dir_len > root && is_sep_for(cwd[dir_len - 1], win)) dir_len--;
    for (;;) {
        text file = {0};
        add_bytes(&file, cwd, dir_len);
        if (dir_len && !is_sep_for(cwd[dir_len - 1], win)) add(&file, win ? "\\" : "/");
        add(&file, "helio.conf");
        size_t length = 0;
        p_result result = file.failed ? P_FAILED : p_read(file.data, 65536, &p->file, &length);
        text_free(&file);
        if (result != P_OK && result != P_MISSING) {
            p_free(cwd);
            return *why = "helio.conf cannot be read", S_DAMAGED;
        }
        if (p->file || dir_len <= root) break;
        while (dir_len > root && !is_sep_for(cwd[dir_len - 1], win)) dir_len--;
        while (dir_len > root && is_sep_for(cwd[dir_len - 1], win)) dir_len--;
    }
    p_free(cwd);
    if (!p->file) return S_OK;
    size_t lineno = 1;
    for (const char *line = p->file; *line; line = next_line(line), lineno++) {
        const char *value = line;
        bool good = true;
        char path[512], lane[36], word[32];
        if (starts(line, "project:")) value += 8, good = take_word(p->project, sizeof p->project, &value) && line_end(value);
        else if (starts(line, "lane:")) value += 5, good = take_word(p->lane, sizeof p->lane, &value) && line_end(value);
        else if (starts(line, "repository:")) value += 11, good = take_word(p->repository, sizeof p->repository, &value) && repo_ok(p->repository) && line_end(value);
        else if (starts(line, "silent:")) {
            value += 7;
            uint64_t mins = 0;
            if (!take_word(word, sizeof word, &value) || !parse_silent_minutes(word, &mins) || !line_end(value)) {
                format_line_error(p->why, sizeof p->why, lineno, "silent must be a positive number of minutes");
                return *why = p->why, S_DAMAGED;
            }
            p->silent_minutes = mins;
        }
        else if (starts(line, "path:")) value += 5, good = take_word(path, sizeof path, &value) && take_word(lane, sizeof lane, &value) && pair_ok("x", lane) && line_end(value);
        else if (*line != '\n' && *line != '\r' && *line != '#') good = false;
        if (!good) {
            format_line_error(p->why, sizeof p->why, lineno, "expected project, lane, repository, silent or path");
            return *why = p->why, S_DAMAGED;
        }
    }
    if (!pair_ok(p->project, *p->lane ? p->lane : "x")) return *why = "helio.conf names no project, or a bad lane", S_DAMAGED;
    p->found = true;
    return S_OK;
}

p_result place_record_silent(store *s, const place *here) {
    if (!s || !s->folder || !here || !here->found || !*here->project) return P_OK;
    text folder = {0}, path = {0}, data = {0}, unique = {0};
    add(&folder, s->folder), add(&folder, "/silent");
    add(&path, folder.data ? folder.data : ""), add(&path, "/"), add(&path, here->project);
    if (folder.failed || path.failed) {
        text_free(&folder), text_free(&path);
        return P_FAILED;
    }
    /* Skip durable write when the stored threshold already matches helio.conf. */
    char *existing = NULL;
    size_t elen = 0;
    p_result er = p_read(path.data, 64, &existing, &elen);
    if (er == P_OK) {
        const char *at = existing;
        char word[32];
        uint64_t cur = 0;
        bool same_val = take_word(word, sizeof word, &at) && parse_silent_minutes(word, &cur) && line_end(at) && cur == here->silent_minutes;
        p_free(existing);
        if (same_val) {
            text_free(&folder), text_free(&path);
            return P_OK;
        }
    }
    add_number(&data, here->silent_minutes, 0), add(&data, "\n");
    add_number(&unique, p_tick(), 0);
    p_result wr = P_FAILED;
    if (!data.failed && !unique.failed && p_folder(folder.data) == P_OK)
        wr = p_write(path.data, data.data, data.length, true, unique.data);
    text_free(&folder), text_free(&path), text_free(&data), text_free(&unique);
    return wr;
}

p_result place_silent_us(const store *s, const place *here, const char *project, uint64_t *out_us) {
    *out_us = (uint64_t)PLACE_SILENT_DEFAULT * 60000000ull;
    if (here && here->found && project && same(here->project, project)) {
        *out_us = here->silent_minutes * 60000000ull;
        return P_OK;
    }
    if (!s || !s->folder || !project || !*project || !pair_ok(project, "x")) return P_OK;
    text path = {0};
    add(&path, s->folder), add(&path, "/silent/"), add(&path, project);
    if (path.failed) {
        text_free(&path);
        return P_FAILED;
    }
    char *data = NULL;
    size_t length = 0;
    p_result r = p_read(path.data, 64, &data, &length);
    text_free(&path);
    if (r == P_MISSING) return P_OK;
    if (r != P_OK) return P_FAILED;
    const char *at = data;
    char word[32];
    uint64_t mins = 0;
    bool ok = take_word(word, sizeof word, &at) && parse_silent_minutes(word, &mins) && line_end(at);
    p_free(data);
    if (!ok) return P_FAILED;
    *out_us = mins * 60000000ull;
    return P_OK;
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

static void session_base(text *out, const char *lane, const char *session, const char *repository) {
    for (; *lane; lane++) add_bytes(out, *lane == '/' ? "+" : lane, 1);
    if (session && *session) add(out, "+"), add_hex(out, session);
    else if (repository && *repository) add(out, "++"), add(out, repository);
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

static void repo_time_upsert(session_info *info, const char *repo, uint64_t when) {
    if (!repo || !*repo || !repo_ok(repo)) return;
    for (size_t i = 0; i < info->repo_count; i++) {
        if (same(info->repos[i].name, repo)) {
            if (when > info->repos[i].when) info->repos[i].when = when;
            return;
        }
    }
    if (info->repo_count < PLACE_MAX_REPOS) {
        size_t i = info->repo_count++;
        copy_str(info->repos[i].name, sizeof info->repos[i].name, repo);
        info->repos[i].when = when;
        return;
    }
    size_t oldest = 0;
    for (size_t i = 1; i < PLACE_MAX_REPOS; i++)
        if (info->repos[i].when < info->repos[oldest].when) oldest = i;
    if (when > info->repos[oldest].when) {
        copy_str(info->repos[oldest].name, sizeof info->repos[oldest].name, repo);
        info->repos[oldest].when = when;
    }
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
    bool have_time = false, have_agent = false, have_repo = false, have_session = false, have_end = false;
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
            have_agent = true;
        } else if (line_len >= 12 && starts(line, "repository: ")) {
            copy_slice(out->repository, sizeof out->repository, line + 12, line_len - 12);
            have_repo = true;
        } else if (line_len >= 9 && starts(line, "session: ")) {
            copy_slice(out->session, sizeof out->session, line + 9, line_len - 9);
            have_session = true;
        } else if (line_len >= 11 && starts(line, "repo-time: ")) {
            size_t sep = 11;
            while (sep < line_len && line[sep] != ' ') sep++;
            if (sep == 11 || sep + 1 >= line_len) return false;
            char rname[36];
            size_t rlen = sep - 11;
            if (rlen >= sizeof rname) return false;
            copy_slice(rname, sizeof rname, line + 11, rlen);
            uint64_t rt = 0;
            if (!repo_ok(rname) || !parse_u64(line + sep + 1, line_len - sep - 1, &rt)) return false;
            repo_time_upsert(out, rname, rt);
        } else if (line_len == 8 && starts(line, "end: yes")) {
            have_end = true;
        }
        pos = end < length ? end + 1 : length;
    }
    return have_time && (have_end || (have_agent && have_repo && have_session && data[length - 1] == '\n'));
}

static void format_session(text *out, uint64_t when, const char *agent, const char *repository, const char *session,
                           const session_info *repos_src) {
    add(out, "time: "), add_number(out, when, 0), add(out, "\n");
    add(out, "agent: "), add(out, agent ? agent : ""), add(out, "\n");
    add(out, "repository: "), add(out, repository ? repository : ""), add(out, "\n");
    add(out, "session: "), add(out, session ? session : ""), add(out, "\n");
    if (repos_src) {
        for (size_t i = 0; i < repos_src->repo_count; i++) {
            add(out, "repo-time: "), add(out, repos_src->repos[i].name), add(out, " ");
            add_number(out, repos_src->repos[i].when, 0), add(out, "\n");
        }
    }
    add(out, "end: yes\n");
}

static p_result read_session_file(const char *path, session_info *out, bool *parsed, bool *empty_file) {
    char *data = NULL;
    size_t length = 0;
    if (empty_file) *empty_file = false;
    p_result r = p_read(path, MOST_SESSION_FILE, &data, &length);
    if (r == P_OK) {
        if (empty_file && length == 0) *empty_file = true;
        *parsed = place_parse_session(data, length, out);
        p_free(data);
    } else {
        *parsed = false;
        *out = (session_info){0};
    }
    return r;
}

/* Validates `<project>+<lane>[+<even-hex>|++<repo>][~<seq>]`. Sets base_len, lane, and seq (0 if no ~<seq>). */
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
        if (plus2 + 1 < blen && name[plus2 + 1] == '+') {
            size_t rlen = blen - plus2 - 2;
            char repo[40];
            if (!rlen || rlen >= sizeof repo) return false;
            copy_slice(repo, sizeof repo, name + plus2 + 2, rlen);
            if (!repo_ok(repo)) return false;
        } else {
            size_t hlen = blen - plus2 - 1;
            if (hlen < 2 || hlen > 128 || (hlen & 1u)) return false;
            for (size_t i = plus2 + 1; i < blen; i++) {
                char c = name[i];
                if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
            }
        }
    }
    *base_len = blen;
    *seq = s;
    return true;
}

typedef struct {
    const char *base;
    size_t base_len;
    uint64_t *seqs;
    size_t count, cap;
    bool failed;
} seq_list;

static bool collect_base_seqs_cb(void *user, const char *name) {
    seq_list *sl = user;
    size_t blen = 0;
    char lane[72];
    uint64_t seq = 0;
    if (!parse_session_filename(name, &blen, lane, sizeof lane, &seq)) return true;
    if (blen != sl->base_len) return true;
    for (size_t i = 0; i < blen; i++)
        if (name[i] != sl->base[i]) return true;
    if (sl->count == sl->cap) {
        size_t next = sl->cap ? sl->cap * 2 : 8;
        uint64_t *grown = p_alloc(next * sizeof *grown);
        if (!grown) { sl->failed = true; return false; }
        for (size_t i = 0; i < sl->count; i++) grown[i] = sl->seqs[i];
        p_free(sl->seqs);
        sl->seqs = grown, sl->cap = next;
    }
    sl->seqs[sl->count++] = seq;
    return true;
}

/* Reads the highest published generation for base. Because p_create_excl publishes
   complete flushed payloads atomically via temp + link (Linux) or MoveFileExW without
   replace (Windows), no current writer ever creates a 0-byte visible generation.
   A 0-byte generation file can therefore only be a dead leftover from an interrupted
   older Windows writer; we skip past 0-byte leftovers to recover an intact predecessor
   (or report only_empty so place_hello can self-heal), while NEVER falling back past
   an I/O read error or a non-empty corrupt/truncated file. */
static p_result read_best_generation(const store *s, const char *base, bool *any_found, uint64_t *max_seq,
                                     bool *valid_found, uint64_t *valid_seq, bool *only_empty, bool verifying, session_info *out) {
    *any_found = false, *max_seq = 0, *valid_found = false, *valid_seq = 0;
    if (only_empty) *only_empty = false;
    *out = (session_info){0};
    text folder = {0};
    add(&folder, s->folder), add(&folder, "/sessions");
    if (folder.failed) {
        text_free(&folder);
        return P_FAILED;
    }
    seq_list sl = {base, len(base), NULL, 0, 0, false};
    p_result lr = p_list(folder.data, collect_base_seqs_cb, &sl);
    text_free(&folder);
    if (lr == P_MISSING) {
        p_free(sl.seqs);
        return P_OK;
    }
    if (lr != P_OK || sl.failed) {
        p_free(sl.seqs);
        return lr != P_OK ? lr : P_FAILED;
    }
    if (!sl.count) {
        p_free(sl.seqs);
        return P_OK;
    }
    for (size_t i = 0; i < sl.count; i++)
        for (size_t j = i + 1; j < sl.count; j++)
            if (sl.seqs[j] > sl.seqs[i]) {
                uint64_t tmp = sl.seqs[i];
                sl.seqs[i] = sl.seqs[j], sl.seqs[j] = tmp;
            }
    *any_found = true;
    *max_seq = sl.seqs[0];
    if (verifying && s->pause) s->pause(s->pause_user, "hello-verify-read");
    bool saw_empty = false;
    for (size_t i = 0; i < sl.count; i++) {
        text path = {0};
        session_seq_path(s, &path, base, sl.seqs[i]);
        if (path.failed) {
            text_free(&path);
            p_free(sl.seqs);
            return P_FAILED;
        }
        bool parsed = false, is_empty = false;
        p_result rr = read_session_file(path.data, out, &parsed, &is_empty);
        text_free(&path);
        if (rr == P_MISSING) {
            /* A listed generation disappeared (swept by a newer publication): force a fresh scan
               instead of falling back to a stale predecessor. */
            p_free(sl.seqs);
            return P_MISSING;
        }
        if (rr != P_OK) {
            p_free(sl.seqs);
            return P_FAILED;
        }
        if (parsed) {
            *valid_found = true;
            *valid_seq = sl.seqs[i];
            p_free(sl.seqs);
            return P_OK;
        }
        if (is_empty) {
            saw_empty = true;
            continue;
        }
        /* Non-empty corrupt/truncated file: refuse without falling back. */
        p_free(sl.seqs);
        return P_OK;
    }
    p_free(sl.seqs);
    if (saw_empty) {
        if (only_empty) *only_empty = true;
        return P_OK;
    }
    return P_OK;
}

/* Lock-free monotonic CAS: each update atomically creates `<base>` (seq 0) or
   `<base>~<seq+1>` via p_create_excl. On P_EXISTS another writer won that slot,
   so we re-read the latest generation, merge monotonically, and retry. After
   publishing, the superseded generation is removed best-effort. */
s_result place_hello(store *s, const char *lane, const char *agent, const char *repository, const char *session) {
    if (!lane || !address_ok(lane, false)) return s->why = "say who you are: --as project/lane or HELIO_AS", S_REFUSED;
    if (!session_ok(session)) return s->why = "the session name is invalid or too long", S_REFUSED;
    if (repository && *repository && !repo_ok(repository)) return s->why = "the repository name is invalid or too long", S_REFUSED;
    const char *ag = agent ? agent : "";
    const char *rp = repository ? repository : "";
    const char *ss = session ? session : "";

    text folder = {0}, base = {0};
    add(&folder, s->folder), add(&folder, "/sessions");
    session_base(&base, lane, ss, rp);
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
        bool any_found = false, have_existing = false, only_empty = false;
        uint64_t max_seq = 0, valid_seq = 0;
        session_info existing = {0};
        p_result br = read_best_generation(s, base.data, &any_found, &max_seq, &have_existing, &valid_seq, &only_empty, false, &existing);
        if (br == P_MISSING) continue; /* Concurrent writer swept all seen generations; retry. */
        if (br != P_OK || (any_found && !have_existing && !only_empty)) {
            text_free(&base);
            return s->why = "the existing session cannot be read", S_DAMAGED;
        }

        uint64_t next_seq = any_found ? max_seq + 1 : 0;
        uint64_t now = p_now();
        if (have_existing && now <= existing.when) now = existing.when + 1;
        const char *use_agent = *ag ? ag : (have_existing && *existing.agent ? existing.agent : "");

        text next_path = {0}, data = {0};
        session_seq_path(s, &next_path, base.data, next_seq);
        session_info merged = {0};
        if (*ss) {
            if (have_existing) {
                for (size_t i = 0; i < existing.repo_count; i++)
                    repo_time_upsert(&merged, existing.repos[i].name, existing.repos[i].when);
                if (*existing.repository) repo_time_upsert(&merged, existing.repository, existing.when);
            }
            if (*rp) repo_time_upsert(&merged, rp, now);
        }
        format_session(&data, now, use_agent, rp, ss, *ss ? &merged : NULL);
        if (next_path.failed || data.failed) {
            text_free(&next_path), text_free(&data), text_free(&base);
            return s->why = "out of memory", S_DAMAGED;
        }

        if (s->pause) s->pause(s->pause_user, "hello-create");

        p_result cr = p_create_excl(next_path.data, data.data, data.length);
        text_free(&data);
        if (cr == P_EXISTS) {
            text_free(&next_path);
            if (s->pause) s->pause(s->pause_user, "hello-retry");
            continue;
        }
        if (cr == P_OK || cr == P_UNFLUSHED) {
            /* Verify our newly written generation next_seq itself is readable and not superseded.
               Retry P_MISSING if a concurrent peer swept next_seq between enumeration and open. */
            bool chk_any = false, chk_valid = false;
            uint64_t chk_max = 0, chk_vseq = 0;
            session_info chk_info = {0};
            p_result chkr = P_MISSING;
            for (int v = 0; v < 8; v++) {
                chkr = read_best_generation(s, base.data, &chk_any, &chk_max, &chk_valid, &chk_vseq, NULL, v == 0, &chk_info);
                if (chkr != P_MISSING) break;
            }
            if (chkr != P_OK || !chk_any || !chk_valid || chk_vseq < next_seq) {
                text_free(&next_path), text_free(&base);
                return s->why = "the session was written, but could not be verified", S_DAMAGED;
            }
            if (chk_vseq > next_seq) {
                p_remove(next_path.data);
                text_free(&next_path);
                if (s->pause) s->pause(s->pause_user, "hello-retry");
                continue;
            }
            if (have_existing) {
                text sweep = {0};
                session_seq_path(s, &sweep, base.data, valid_seq);
                if (!sweep.failed) p_remove(sweep.data);
                text_free(&sweep);
            }
            if (any_found && (!have_existing || max_seq != valid_seq)) {
                text empty_sweep = {0};
                session_seq_path(s, &empty_sweep, base.data, max_seq);
                if (!empty_sweep.failed) p_remove(empty_sweep.data);
                text_free(&empty_sweep);
            }
            text_free(&next_path), text_free(&base);
            if (cr == P_UNFLUSHED) return s->why = "the session was written, but its folder could not be flushed", S_DAMAGED;
            return S_OK;
        }
        text_free(&next_path), text_free(&base);
        return s->why = "the session cannot be announced", S_DAMAGED;
    }

    text_free(&base);
    return s->why = "the session cannot be announced", S_DAMAGED;
}

s_result place_session(store *s, const char *lane, const char *session, session_info *out) {
    *out = (session_info){0};
    if (!lane || !address_ok(lane, false) || !session_ok(session)) return s->why = "invalid lane or session", S_REFUSED;
    text base = {0};
    session_base(&base, lane, session ? session : "", "");
    if (base.failed) {
        text_free(&base);
        return s->why = "out of memory", S_DAMAGED;
    }
    for (int attempt = 0; attempt < 8; attempt++) {
        bool any_found = false, valid_found = false;
        uint64_t max_seq = 0, valid_seq = 0;
        p_result br = read_best_generation(s, base.data, &any_found, &max_seq, &valid_found, &valid_seq, NULL, false, out);
        if (br == P_MISSING) continue;
        text_free(&base);
        if (br == P_OK && !any_found) return S_NOTHING;
        if (br != P_OK || !valid_found) return s->why = "the session cannot be read", S_DAMAGED;
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
    const char *only_lane;
    bool failed;
} latest_map;

static bool collect_latest_cb(void *user, const char *name) {
    latest_map *m = user;
    size_t blen = 0;
    char lane[72];
    uint64_t seq = 0;
    if (!parse_session_filename(name, &blen, lane, sizeof lane, &seq)) return true;
    if (m->only_lane && !same(lane, m->only_lane)) return true;
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

static p_result each_session_filtered(store *s, const char *only_lane, bool (*each)(void *user, const session_info *info), void *user) {
    text folder = {0};
    add(&folder, s->folder), add(&folder, "/sessions");
    if (folder.failed) {
        text_free(&folder);
        return P_FAILED;
    }
    latest_map map = {.only_lane = only_lane};
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
        bool ok = false, gone = false, rescan = false;
        text fast = {0};
        session_seq_path(s, &fast, map.items[i].base, map.items[i].seq);
        if (fast.failed) {
            text_free(&fast);
            had_error = true;
            continue;
        }
        bool parsed = false, is_empty = false;
        p_result rr = read_session_file(fast.data, &info, &parsed, &is_empty);
        text_free(&fast);
        if (rr == P_OK && parsed) {
            copy_str(info.lane, sizeof info.lane, map.items[i].lane);
            ok = true;
        } else if (rr == P_MISSING || (rr == P_OK && is_empty)) {
            rescan = true;
        } else {
            had_error = true;
        }
        if (rescan) {
            for (int attempt = 0; attempt < 8; attempt++) {
                bool any_found = false, valid_found = false;
                uint64_t max_seq = 0, valid_seq = 0;
                p_result br = read_best_generation(s, map.items[i].base, &any_found, &max_seq, &valid_found, &valid_seq, NULL, false, &info);
                if (br == P_MISSING) continue;
                if (br == P_OK && !any_found) {
                    gone = true;
                    break;
                }
                if (br == P_OK && valid_found) {
                    copy_str(info.lane, sizeof info.lane, map.items[i].lane);
                    ok = true;
                } else {
                    had_error = true;
                }
                break;
            }
            if (!ok && !gone) had_error = true;
        }
        if (ok) keep = each(user, &info);
    }
    for (size_t i = 0; i < map.count; i++) p_free(map.items[i].base);
    p_free(map.items);
    return had_error ? P_FAILED : P_OK;
}

p_result place_each_session(store *s, bool (*each)(void *user, const session_info *info), void *user) {
    return each_session_filtered(s, NULL, each, user);
}

p_result place_lane_sessions(store *s, const char *lane, bool (*each)(void *user, const session_info *info), void *user) {
    return each_session_filtered(s, lane, each, user);
}

typedef struct { const char *project, *repository, *me; uint64_t since; bool busy; } look;

static bool busy_each(void *user, const session_info *info) {
    look *l = user;
    size_t n = len(l->project);
    if (!starts(info->lane, l->project) || info->lane[n] != '/') return true;
    if (same(info->lane, l->me)) return true;
    if (l->repository && *l->repository && !same(info->repository, l->repository)) return true;
    if (info->when >= l->since) l->busy = true;
    return !l->busy;
}

s_result place_busy(store *s, const char *project, const char *repository, const char *me, uint64_t within, bool *busy) {
    *busy = false;
    uint64_t now = p_now();
    look l = {project, repository, me, now > within ? now - within : 0, false};
    p_result r = place_each_session(s, busy_each, &l);
    if (r == P_MISSING) return S_OK;
    if (r != P_OK) return s->why = "the sessions folder cannot be read", S_DAMAGED;
    *busy = l.busy;
    return S_OK;
}
