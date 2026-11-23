#include "place.h"

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
        else if (starts(line, "path:")) value += 5, good = take_word(path, sizeof path, &value) && take_word(lane, sizeof lane, &value) && pair_ok("x", lane);
        else if (*line != '\n' && *line != '\r' && *line != '#') good = false;
        if (!good) return *why = "helio.conf has a line that is not project, lane or path", S_DAMAGED;
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
            size_t i = 0;
            do winner[i] = lane[i];
            while (lane[i++]);
        }
    }
    if (*winner) add(to, p->project), add(to, "/"), add(to, winner);
}

static void hello_path(const store *s, text *out, const char *lane) {
    add(out, s->folder), add(out, "/sessions/");
    for (; *lane; lane++) add_bytes(out, *lane == '/' ? "+" : lane, 1);
}

s_result place_hello(store *s, const char *lane) {
    text folder = {0}, path = {0}, data = {0}, unique = {0};
    add(&folder, s->folder), add(&folder, "/sessions"), hello_path(s, &path, lane);
    add_number(&data, p_now(), 0), add(&data, "\n"), add_number(&unique, p_tick(), 0);
    bool full = folder.failed || path.failed || data.failed || unique.failed;
    p_result result = full ? P_FAILED : p_folder(folder.data);
    if (result == P_OK) result = p_write(path.data, data.data, data.length, true, unique.data);
    text_free(&folder), text_free(&path), text_free(&data), text_free(&unique);
    if (result != P_OK) s->why = "the session cannot be announced";
    return result == P_OK ? S_OK : S_DAMAGED;
}

typedef struct { store *s; const char *project, *me; uint64_t since; bool busy; } look;

static bool seen_one(void *user, const char *name) {
    look *l = user;
    size_t n = len(l->project);
    if (!starts(name, l->project) || name[n] != '+') return true;
    for (const char *c = name; *c; c++) if (*c == '.') return true; /* being written */
    bool self = true;
    for (size_t i = 0; self; i++) {
        self = name[i] == (l->me[i] == '/' ? '+' : l->me[i]);
        if (!name[i]) break;
    }
    if (self) return true;
    text path = {0};
    add(&path, l->s->folder), add(&path, "/sessions/"), add(&path, name);
    char *data = NULL;
    size_t length;
    if (!path.failed && p_read(path.data, 64, &data, &length) == P_OK) {
        uint64_t when = 0;
        for (const char *d = data; *d >= '0' && *d <= '9'; d++) when = when * 10 + (uint64_t)(*d - '0');
        l->busy = l->busy || when >= l->since;
        p_free(data);
    }
    text_free(&path);
    return !l->busy;
}

bool place_busy(store *s, const char *project, const char *me, uint64_t within) {
    uint64_t now = p_now();
    look l = {s, project, me, now > within ? now - within : 0, false};
    text folder = {0};
    add(&folder, s->folder), add(&folder, "/sessions");
    if (!folder.failed) p_list(folder.data, seen_one, &l);
    text_free(&folder);
    return l.busy;
}
