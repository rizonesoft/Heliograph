#include "archive.h"

static const uint64_t ARCHIVE_AGE_US = 30ull * 86400ull * 1000000ull;
static const char ARCHIVE_SEEN[] = "seen\n";
#ifdef HELIO_TEST_HOOKS
size_t archive_batch_size = 2048;
#endif

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

/* The test pause for archive folder operations, set by archive_pauses; NULL in production. */
static void (*arch_pause_fn)(void *user, const char *stage);
static void *arch_pause_user;

void archive_pauses(void (*pause)(void *user, const char *stage), void *user) { arch_pause_fn = pause, arch_pause_user = user; }

static void arch_pause(const char *stage) {
    if (arch_pause_fn && stage) arch_pause_fn(arch_pause_user, stage);
}

/* A file under archive/<month>/ (or, with no month, under archive/), named inside its folder rather than by a
   full path: every operation holds the folder through p_dir_open, which refuses a symlink or junction in place of
   archive/ or the month folder, and acts on the name inside the held folder. */
typedef struct {
    const char *folder;
    const char *month;
    text name;
} arch_at;

static void arch_at_of(arch_at *a, const char *folder, const char *month, const char *id, const char *event) {
    a->folder = folder, a->month = month, a->name = (text){0};
    add(&a->name, id), add(&a->name, "."), add(&a->name, event);
}

static void arch_at_free(arch_at *a) { text_free(&a->name); }

/* Holds archive/ (month NULL) or archive/<month>/ under the store folder. */
static p_result arch_hold(const char *folder, const char *month, p_dir **out) {
    text rel = {0};
    add(&rel, "archive");
    if (month) add(&rel, "/"), add(&rel, month);
    p_result r = rel.failed ? P_FAILED : p_dir_open(folder, rel.data, out);
    text_free(&rel);
    return r;
}

static p_result arch_read_at(const arch_at *a, size_t most, char **data, size_t *length, const char *stage) {
    p_dir *d = NULL;
    p_result r = a->name.failed ? P_FAILED : arch_hold(a->folder, a->month, &d);
    if (r != P_OK) return r;
    arch_pause(stage);
    r = p_dir_read(d, a->name.data, most, data, length);
    p_dir_close(d);
    return r;
}

static p_result arch_read(const arch_at *a, size_t most, char **data, size_t *length) { return arch_read_at(a, most, data, length, NULL); }

static p_result arch_write(const arch_at *a, const char *data, size_t length, bool replace, const char *unique, const char *stage) {
    p_dir *d = NULL;
    p_result r = a->name.failed ? P_FAILED : arch_hold(a->folder, a->month, &d);
    if (r != P_OK) return r;
    arch_pause(stage);
    r = p_dir_write(d, a->name.data, data, length, replace, unique);
    p_dir_close(d);
    return r;
}

static p_result arch_sync(const arch_at *a) {
    p_dir *d = NULL;
    p_result r = a->name.failed ? P_FAILED : arch_hold(a->folder, a->month, &d);
    if (r != P_OK) return r;
    r = p_dir_sync(d, a->name.data);
    p_dir_close(d);
    return r;
}

/* Lists archive/ (month NULL) or archive/<month>/ through the held folder. */
static p_result arch_list(const char *folder, const char *month, bool (*each)(void *user, const char *name), void *user, const char *stage) {
    p_dir *d = NULL;
    p_result r = arch_hold(folder, month, &d);
    if (r != P_OK) return r;
    arch_pause(stage);
    r = p_dir_list(d, each, user);
    p_dir_close(d);
    return r;
}

static void link_suffix(text *out, unsigned n) {
    add(out, "link."), add_number(out, n, 4);
}

static bool parse_link_index(const char *ev, unsigned *out) {
    if (!starts(ev, "link.")) return false;
    const char *p = ev + 5;
    if (!*p) return false;
    unsigned v = 0;
    for (; *p; p++) {
        if (*p < '0' || *p > '9' || v > 100000) return false;
        v = v * 10 + (unsigned)(*p - '0');
    }
    *out = v;
    return true;
}

/* Holds archive/<month>/ once to learn whether it is there: S_OK (a real folder below a real archive/), S_NOTHING
   (either is absent) or S_DAMAGED (a symlink, a junction or not a folder). */
static s_result check_arch_dirs(const char *folder, const char *month) {
    p_dir *d = NULL;
    p_result r = arch_hold(folder, month, &d);
    p_dir_close(d);
    return r == P_OK ? S_OK : r == P_MISSING ? S_NOTHING : S_DAMAGED;
}

s_result archive_read_message(store *s, const char *id, message *out) {
    *out = (message){0};
    char month[8];
    if (!valid_id(id) || !archive_month(id, month)) return S_NOTHING;
    s_result dr = check_arch_dirs(s->folder, month);
    if (dr == S_NOTHING) return S_NOTHING;
    if (dr != S_OK) {
        s->why = "the archive folder is not a real directory";
        return S_DAMAGED;
    }
    arch_at at = {0};
    arch_at_of(&at, s->folder, month, id, "post");
    char *data = NULL;
    size_t length = 0;
    p_result r = at.name.failed ? P_FAILED : arch_read_at(&at, MOST_FILE, &data, &length, "arch-held");
    arch_at_free(&at);
    if (r == P_MISSING) return S_NOTHING;
    if (r != P_OK) {
        s->why = "an archived message cannot be read";
        return S_DAMAGED;
    }
    s_result pr = store_parse_post(data, length, id, out, &s->why);
    if (pr == S_OK) out->archived = true;
    return pr;
}

s_result archive_has_terminal_job(const char *folder, const char *id, unsigned *links_out) {
    if (links_out) *links_out = 0;
    char month[8];
    if (!valid_id(id) || !archive_month(id, month)) return S_NOTHING;
    store tmp = {.folder = folder};
    message m = {0};
    s_result mr = archive_read_message(&tmp, id, &m);
    if (mr != S_OK) {
        message_free(&m);
        return mr;
    }
    if (!is_job(&m)) {
        message_free(&m);
        return S_DAMAGED;
    }
    job now = {.state = OPEN};
    bool ok = true;
    for (; now.links < MOST_LINKS; now.links++) {
        text ev = {0};
        arch_at at = {0};
        link_suffix(&ev, now.links);
        if (!ev.failed) arch_at_of(&at, folder, month, id, ev.data);
        char *data = NULL;
        size_t length = 0;
        p_result r = ev.failed || at.name.failed ? P_FAILED : arch_read(&at, MOST_FILE, &data, &length);
        text_free(&ev), arch_at_free(&at);
        if (r == P_MISSING) break;
        if (r != P_OK || len(data) != length) {
            if (r == P_OK) p_free(data);
            ok = false;
            break;
        }
        bool good = store_apply_step(&m, &now, data);
        p_free(data);
        if (!good) {
            ok = false;
            break;
        }
    }
    message_free(&m);
    if (!ok) return S_DAMAGED;
    if (now.state != DONE && now.state != REFUSED) return S_REFUSED;
    if (links_out) *links_out = now.links;
    return S_OK;
}

bool archive_is_clean_remnant(const char *folder, const char *id, const char *event, const char *data, size_t length) {
    unsigned arch_links = 0;
    if (archive_has_terminal_job(folder, id, &arch_links) != S_OK) return false;
    if (starts(event, "seen.")) return store_valid_seen_event(event) && length == len(ARCHIVE_SEEN) && same(data, ARCHIVE_SEEN);
    if (starts(event, "landed.")) {
        if (!store_valid_landed_event(event) || !store_valid_landed_payload(data, length, event + 7)) return false;
        char month[8];
        if (!archive_month(id, month)) return false;
        arch_at dst = {0};
        arch_at_of(&dst, folder, month, id, event);
        char *adata = NULL;
        size_t alen = 0;
        p_result r = dst.name.failed ? P_FAILED : arch_read(&dst, MOST_FILE, &adata, &alen);
        arch_at_free(&dst);
        bool equal = r == P_OK && alen == length;
        for (size_t i = 0; equal && i < length; i++) equal = adata[i] == data[i];
        p_free(adata);
        return equal;
    }
    unsigned idx = 0;
    if (!parse_link_index(event, &idx)) return false;
    if (idx >= arch_links) return true; /* late step from a writer that raced post removal */
    char month[8];
    if (!archive_month(id, month)) return false;
    arch_at dst = {0};
    arch_at_of(&dst, folder, month, id, event);
    char *adata = NULL;
    size_t alen = 0;
    p_result r = dst.name.failed ? P_FAILED : arch_read(&dst, MOST_FILE, &adata, &alen);
    arch_at_free(&dst);
    bool equal = r == P_OK && alen == length;
    for (size_t i = 0; equal && i < length; i++) equal = adata[i] == data[i];
    p_free(adata);
    return equal;
}

static s_result copy_durable_ex(const char *src, const arch_at *dst, const char **why, bool *created,
                                void (*pause)(void *user, const char *stage), void *pause_user, const char *after_read_stage,
                                const char *write_stage) {
    if (created) *created = false;
    char *data = NULL;
    size_t length = 0;
    p_result r = p_read(src, MOST_FILE, &data, &length);
    if (r == P_MISSING) {
        char *there = NULL;
        size_t tlen = 0;
        p_result dr = arch_read(dst, MOST_FILE, &there, &tlen);
        p_free(there);
        if (dr == P_OK) return arch_sync(dst) == P_OK ? S_NOTHING : (*why = "an archive copy cannot be flushed", S_DAMAGED);
        *why = "a live file vanished before its archive copy was present";
        return S_DAMAGED;
    }
    if (r != P_OK) {
        *why = "a live file cannot be read";
        return S_DAMAGED;
    }
    if (pause && after_read_stage) pause(pause_user, after_read_stage);
    text unique = {0};
    add_number(&unique, p_tick(), 0);
    p_result w = unique.failed ? P_FAILED : arch_write(dst, data, length, false, unique.data, write_stage);
    text_free(&unique);
    if (w == P_OK) {
        p_free(data);
        if (arch_sync(dst) != P_OK) {
            *why = "an archive copy cannot be flushed";
            return S_DAMAGED;
        }
        if (created) *created = true;
        return S_OK;
    }
    if (w == P_EXISTS) {
        char *there = NULL;
        size_t tlen = 0;
        p_result dr = arch_read(dst, MOST_FILE, &there, &tlen);
        bool equal = dr == P_OK && tlen == length;
        for (size_t i = 0; equal && i < length; i++) equal = there[i] == data[i];
        p_free(there);
        p_free(data);
        if (!equal) {
            *why = "archive copy differs from live file";
            return S_DAMAGED;
        }
        if (arch_sync(dst) != P_OK) {
            *why = "an archive copy cannot be flushed";
            return S_DAMAGED;
        }
        return S_OK;
    }
    p_free(data);
    *why = w == P_UNFLUSHED ? "an archive copy cannot be flushed" : "an archive copy cannot be written";
    return S_DAMAGED;
}

static s_result copy_durable(const char *src, const arch_at *dst, const char **why) {
    return copy_durable_ex(src, dst, why, NULL, NULL, NULL, NULL, "before-arch-copy");
}

static s_result copy_durable_paused(const char *src, const arch_at *dst, const char **why,
                                    void (*pause)(void *user, const char *stage), void *pause_user, const char *after_read_stage) {
    return copy_durable_ex(src, dst, why, NULL, pause, pause_user, after_read_stage, "before-arch-copy");
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
        size_t cap = l->capacity ? l->capacity * 2 : 16;
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
    char *id;
    char month[8];
    str_list seen;
    str_list landed;
    str_list links;
    str_list rm_parts;
    unsigned arch_links;
    bool post_removed;
    bool did_unlink;
    bool peer_moved;
} batch_item;

typedef struct {
    batch_item *items;
    size_t count;
    bool failed;
} batch_events;

static int find_batch_id(const batch_events *be, const char *id, size_t id_len) {
    size_t low = 0, high = be->count;
    while (low < high) {
        size_t mid = low + (high - low) / 2;
        const char *cand = be->items[mid].id;
        int ord = 0;
        for (size_t i = 0; i < id_len && !ord; i++) ord = (unsigned char)cand[i] - (unsigned char)id[i];
        if (!ord) ord = (unsigned char)cand[id_len];
        if (!ord) return (int)mid;
        if (ord < 0) low = mid + 1;
        else high = mid;
    }
    return -1;
}

static bool is_rm_part(const char *name, size_t n) {
    return n > 8 && same(name + n - 8, ".rm.part");
}

static bool is_post_part_ev(const char *ev, size_t n) {
    return n > 10 && starts(ev, "post.") && same(ev + n - 5, ".part");
}

static bool is_post_part_name(const char *name, size_t n) {
    if (n <= 10 || !same(name + n - 5, ".part")) return false;
    const char *dot = name;
    while (*dot && *dot != '.') dot++;
    if (!*dot) return false;
    return starts(dot + 1, "post.");
}

static bool attach_events(void *user, const char *name) {
    batch_events *be = user;
    size_t n = len(name);
    if (n > 5 && same(name + n - 5, ".part") && !is_post_part_name(name, n)) return true;
    const char *dot = name;
    while (*dot && *dot != '.') dot++;
    if (!*dot) return true;
    size_t id_len = (size_t)(dot - name);
    int idx = find_batch_id(be, name, id_len);
    if (idx < 0) return true;
    const char *ev = dot + 1;
    if (same(ev, "post")) return true;
    if (starts(ev, "seen.")) list_push(&be->items[idx].seen, ev, len(ev));
    else if (same(ev, "landed") || starts(ev, "landed.")) list_push(&be->items[idx].landed, ev, len(ev));
    else if (starts(ev, "link.")) list_push(&be->items[idx].links, ev, len(ev));
    else if (is_post_part_ev(ev, len(ev))) list_push(&be->items[idx].rm_parts, ev, len(ev));
    if (be->items[idx].seen.failed || be->items[idx].landed.failed || be->items[idx].links.failed || be->items[idx].rm_parts.failed) return be->failed = true, false;
    return true;
}

static bool populate_batch_events(const char *folder, batch_events *be) {
    for (size_t i = 0; i < be->count; i++) {
        list_free(&be->items[i].seen);
        list_free(&be->items[i].landed);
        list_free(&be->items[i].links);
        list_free(&be->items[i].rm_parts);
    }
    be->failed = false;
    text dir = {0};
    add(&dir, folder), add(&dir, "/messages");
    p_result r = dir.failed ? P_FAILED : p_list(dir.data, attach_events, be);
    text_free(&dir);
    if (r != P_OK || be->failed) return false;
    for (size_t i = 0; i < be->count; i++) {
        sort_list(&be->items[i].seen);
        sort_list(&be->items[i].landed);
        sort_list(&be->items[i].links);
    }
    return true;
}

static void add_report(text *report, const char *id, const char *why) {
    if (!report) return;
    text clean_id = {0};
    clean(&clean_id, id, true);
    add(report, clean_id.data ? clean_id.data : "?"), add(report, ": "), add(report, why ? why : "damaged"), add(report, "\n");
    text_free(&clean_id);
}

/* Makes archive/ in the store folder, then the month folder inside the held archive/, so a swap of archive/ for a
   link between the two is refused rather than followed. */
static bool ensure_archive_month(const char *folder, const char *month) {
    text a = {0};
    add(&a, folder), add(&a, "/archive");
    bool ok = !a.failed && p_folder(a.data) == P_OK;
    text_free(&a);
    if (!ok) return false;
    arch_pause("before-make-month");
    p_dir *d = NULL;
    ok = arch_hold(folder, NULL, &d) == P_OK && p_dir_folder(d, month) == P_OK;
    p_dir_close(d);
    return ok;
}

/* Verifies and removes live .seen.* (copying valid late marks first) and live .link.* (last-to-first,
   verifying byte-equality against archive copies before removing). */
static s_result sweep_item_tail(const char *folder, const batch_item *it,
                                void (*pause)(void *user, const char *stage), void *pause_user, const char **why) {
    s_result res = S_OK;
    for (size_t i = 0; res == S_OK && i < it->seen.count; i++) {
        text src = {0};
        arch_at dst = {0};
        live_path(folder, &src, it->id, it->seen.items[i]);
        arch_at_of(&dst, folder, it->month, it->id, it->seen.items[i]);
        char *data = NULL;
        size_t length = 0;
        p_result sr = src.failed || dst.name.failed ? P_FAILED : p_read(src.data, MOST_FILE, &data, &length);
        if (sr == P_OK) {
            bool valid_seen = store_valid_seen_event(it->seen.items[i]) && length == len(ARCHIVE_SEEN) && same(data, ARCHIVE_SEEN);
            p_free(data);
            if (!valid_seen) {
                *why = "a seen mark is damaged";
                res = S_DAMAGED;
            } else {
                s_result cr = copy_durable(src.data, &dst, why);
                if (cr == S_DAMAGED) res = S_DAMAGED;
                else {
                    if (pause) pause(pause_user, "before-remove-seen");
                    p_result rm = p_remove(src.data);
                    if (rm != P_OK && rm != P_MISSING) *why = "a seen mark could not be removed", res = S_DAMAGED;
                }
            }
        } else if (sr != P_MISSING) {
            *why = "a seen mark cannot be read";
            res = S_DAMAGED;
        }
        text_free(&src), arch_at_free(&dst);
    }
    if (res == S_OK && pause) pause(pause_user, "after-remove-seen");
    for (size_t i = 0; res == S_OK && i < it->landed.count; i++) {
        const char *ev = it->landed.items[i];
        text src = {0};
        arch_at dst = {0};
        live_path(folder, &src, it->id, ev);
        arch_at_of(&dst, folder, it->month, it->id, ev);
        char *data = NULL;
        size_t length = 0;
        p_result sr = src.failed || dst.name.failed ? P_FAILED : p_read(src.data, MOST_FILE, &data, &length);
        if (sr == P_OK) {
            bool valid_landed = store_valid_landed_event(ev) && store_valid_landed_payload(data, length, ev + 7);
            p_free(data);
            if (!valid_landed) {
                *why = "a landed mark is damaged";
                res = S_DAMAGED;
            } else {
                s_result cr = copy_durable_paused(src.data, &dst, why, pause, pause_user, "after-read-landed");
                if (cr == S_DAMAGED) res = S_DAMAGED;
                else {
                    if (pause) pause(pause_user, "before-remove-landed");
                    p_result rm = p_remove(src.data);
                    if (rm != P_OK && rm != P_MISSING) *why = "a landed mark could not be removed", res = S_DAMAGED;
                }
            }
        } else if (sr != P_MISSING) {
            *why = "a landed mark cannot be read";
            res = S_DAMAGED;
        }
        text_free(&src), arch_at_free(&dst);
    }
    for (size_t i = it->links.count; res == S_OK && i-- > 0;) {
        const char *ev = it->links.items[i];
        text src = {0};
        arch_at dst = {0};
        live_path(folder, &src, it->id, ev);
        arch_at_of(&dst, folder, it->month, it->id, ev);
        char *ldata = NULL;
        size_t llen = 0;
        p_result lr = src.failed || dst.name.failed ? P_FAILED : p_read(src.data, MOST_FILE, &ldata, &llen);
        if (lr == P_MISSING) {
            text_free(&src), arch_at_free(&dst);
            continue;
        }
        if (lr != P_OK) {
            *why = "a live step cannot be read";
            res = S_DAMAGED;
            text_free(&src), arch_at_free(&dst);
            break;
        }
        unsigned idx = 0;
        bool known_idx = parse_link_index(ev, &idx);
        if (!known_idx) {
            p_free(ldata);
            *why = "a step name is damaged";
            res = S_DAMAGED;
            text_free(&src), arch_at_free(&dst);
            break;
        }
        if (idx < it->arch_links) {
            char *adata = NULL;
            size_t alen = 0;
            p_result ar = arch_read(&dst, MOST_FILE, &adata, &alen);
            bool equal = ar == P_OK && alen == llen;
            for (size_t k = 0; equal && k < llen; k++) equal = adata[k] == ldata[k];
            p_free(adata);
            if (!equal) {
                p_free(ldata);
                *why = "archive copy differs from live step";
                res = S_DAMAGED;
                text_free(&src), arch_at_free(&dst);
                break;
            }
            if (arch_sync(&dst) != P_OK) {
                p_free(ldata);
                *why = "an archive copy cannot be flushed";
                res = S_DAMAGED;
                text_free(&src), arch_at_free(&dst);
                break;
            }
        }
        p_free(ldata);
        if (pause) pause(pause_user, "before-remove-link");
        p_result rm = p_remove(src.data);
        text_free(&src), arch_at_free(&dst);
        if (rm != P_OK && rm != P_MISSING) *why = "a step could not be removed", res = S_DAMAGED;
    }
    for (size_t i = 0; res == S_OK && i < it->rm_parts.count; i++) {
        text src = {0};
        live_path(folder, &src, it->id, it->rm_parts.items[i]);
        p_result rm = src.failed ? P_FAILED : p_remove(src.data);
        text_free(&src);
        if (rm != P_OK && rm != P_MISSING) *why = "an interrupted removal could not be cleaned up", res = S_DAMAGED;
    }
    return res;
}

typedef struct {
    str_list ids;
    const char *after;
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
    if (n > 5 && same(name + n - 5, ".part") && !is_post_part_name(name, n)) return true;
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

static p_result remove_post_exclusive(const char *path) {
    text uniq = {0};
    add_number(&uniq, p_tick(), 0);
    p_result r = uniq.failed ? P_FAILED : p_claim_remove(path, uniq.data);
    text_free(&uniq);
    return r;
}

/* Prepares and copies one item (and removes its live .post). Leaves live tail sweep for the single batch pass! */
static s_result copy_and_unlink_post(const char *folder, batch_item *it, uint64_t now,
                                     text *report, void (*pause)(void *user, const char *stage), void *pause_user) {
    const char *why = NULL;
    if (!valid_id(it->id)) {
        add_report(report, it->id, "that is not a message id");
        return S_DAMAGED;
    }
    if (!archive_month(it->id, it->month)) {
        add_report(report, it->id, "the message id has no valid date");
        return S_DAMAGED;
    }
    text post_p = {0};
    live_path(folder, &post_p, it->id, "post");
    char *pdata = NULL;
    size_t plen = 0;
    p_result pr = post_p.failed ? P_FAILED : p_read(post_p.data, MOST_FILE, &pdata, &plen);
    if (pr == P_MISSING) {
        text_free(&post_p);
        unsigned alinks = 0;
        s_result tr = archive_has_terminal_job(folder, it->id, &alinks);
        if (tr != S_OK) {
            /* Live link/seen/rm.part without live post and without a validated archived terminal job is genuine damage!
               An ordinary <id>.post.<u >.part without an archived job is a live store_post in flight and is ignored. */
            bool has_rm_part = false;
            for (size_t i = 0; i < it->rm_parts.count; i++)
                if (is_rm_part(it->rm_parts.items[i], len(it->rm_parts.items[i]))) has_rm_part = true;
            if (tr == S_DAMAGED || it->links.count > 0 || it->seen.count > 0 || it->landed.count > 0 || has_rm_part) {
                add_report(report, it->id, tr == S_DAMAGED ? "archived terminal chain is damaged or incomplete" : "orphan event without post");
                return S_DAMAGED;
            }
            return S_NOTHING;
        }
        if (!ensure_archive_month(folder, it->month)) {
            add_report(report, it->id, "archive folder cannot be created");
            return S_DAMAGED;
        }
        it->arch_links = alinks;
        it->peer_moved = true;
        it->post_removed = true;
        return S_OK;
    }
    if (pr != P_OK) {
        text_free(&post_p);
        add_report(report, it->id, "the message cannot be read");
        return S_DAMAGED;
    }
    /* If an archived terminal copy ALREADY exists (e.g. a killed keyed retry left a duplicate live post,
       or an earlier archive run stopped after publishing terminal links), verify and flush the archived post,
       every live step, and every live seen mark BEFORE removing the live post. */
    unsigned existing_links = 0;
    if (archive_has_terminal_job(folder, it->id, &existing_links) == S_OK) {
        if (!ensure_archive_month(folder, it->month)) {
            p_free(pdata);
            text_free(&post_p);
            add_report(report, it->id, "archive folder cannot be created");
            return S_DAMAGED;
        }
        arch_at dst_post = {0};
        arch_at_of(&dst_post, folder, it->month, it->id, "post");
        char *adata = NULL;
        size_t alen = 0;
        p_result ar = dst_post.name.failed ? P_FAILED : arch_read(&dst_post, MOST_FILE, &adata, &alen);
        bool equal = ar == P_OK && alen == plen;
        for (size_t k = 0; equal && k < plen; k++) equal = adata[k] == pdata[k];
        p_free(adata);
        p_free(pdata);
        if (!equal) {
            arch_at_free(&dst_post), text_free(&post_p);
            add_report(report, it->id, "archive copy differs from live file");
            return S_DAMAGED;
        }
        if (arch_sync(&dst_post) != P_OK) {
            arch_at_free(&dst_post), text_free(&post_p);
            add_report(report, it->id, "an archive copy cannot be flushed");
            return S_DAMAGED;
        }
        arch_at_free(&dst_post);
        /* Verify, copy, and flush every remaining live step BEFORE unlinking the live post. */
        for (size_t i = 0; i < it->links.count; i++) {
            text src = {0};
            arch_at dst = {0};
            live_path(folder, &src, it->id, it->links.items[i]);
            arch_at_of(&dst, folder, it->month, it->id, it->links.items[i]);
            s_result cr = (src.failed || dst.name.failed) ? S_DAMAGED : copy_durable(src.data, &dst, &why);
            text_free(&src), arch_at_free(&dst);
            if (cr == S_DAMAGED) {
                text_free(&post_p);
                add_report(report, it->id, why ? why : "an archive step cannot be preserved");
                return S_DAMAGED;
            }
        }
        /* Validate, copy, and flush every remaining live seen mark BEFORE unlinking the live post. */
        for (size_t i = 0; i < it->seen.count; i++) {
            text src = {0};
            arch_at dst = {0};
            live_path(folder, &src, it->id, it->seen.items[i]);
            char *sd = NULL;
            size_t sl = 0;
            p_result sr = src.failed ? P_FAILED : p_read(src.data, MOST_FILE, &sd, &sl);
            if (sr == P_MISSING) {
                text_free(&src);
                continue;
            }
            bool ok_seen = sr == P_OK && store_valid_seen_event(it->seen.items[i]) && sl == len(ARCHIVE_SEEN) && same(sd, ARCHIVE_SEEN);
            p_free(sd);
            if (!ok_seen) {
                text_free(&src), text_free(&post_p);
                add_report(report, it->id, "a seen mark is damaged");
                return S_DAMAGED;
            }
            arch_at_of(&dst, folder, it->month, it->id, it->seen.items[i]);
            s_result cr = dst.name.failed ? S_DAMAGED : copy_durable(src.data, &dst, &why);
            text_free(&src), arch_at_free(&dst);
            if (cr == S_DAMAGED) {
                text_free(&post_p);
                add_report(report, it->id, why ? why : "a seen mark cannot be preserved");
                return S_DAMAGED;
            }
        }
        /* Validate, copy, and flush every remaining live landed mark BEFORE unlinking the live post. */
        for (size_t i = 0; i < it->landed.count; i++) {
            const char *ev = it->landed.items[i];
            text src = {0};
            arch_at dst = {0};
            live_path(folder, &src, it->id, ev);
            char *ld = NULL;
            size_t ll = 0;
            p_result lr = src.failed ? P_FAILED : p_read(src.data, MOST_FILE, &ld, &ll);
            if (lr == P_MISSING) {
                text_free(&src);
                continue;
            }
            bool ok_landed = lr == P_OK && store_valid_landed_event(ev) && store_valid_landed_payload(ld, ll, ev + 7);
            p_free(ld);
            if (!ok_landed) {
                text_free(&src), text_free(&post_p);
                add_report(report, it->id, "a landed mark is damaged");
                return S_DAMAGED;
            }
            arch_at_of(&dst, folder, it->month, it->id, ev);
            s_result cr = dst.name.failed ? S_DAMAGED : copy_durable(src.data, &dst, &why);
            text_free(&src), arch_at_free(&dst);
            if (cr == S_DAMAGED) {
                text_free(&post_p);
                add_report(report, it->id, why ? why : "a landed mark cannot be preserved");
                return S_DAMAGED;
            }
        }
        p_result rm_post = remove_post_exclusive(post_p.data);
        text_free(&post_p);
        if (rm_post != P_OK && rm_post != P_MISSING) {
            add_report(report, it->id, "the post could not be removed");
            return S_DAMAGED;
        }
        it->peer_moved = (rm_post == P_MISSING);
        it->arch_links = existing_links;
        it->post_removed = true;
        it->did_unlink = (rm_post == P_OK);
        return S_OK;
    }
    message m = {0};
    if (store_parse_post(pdata, plen, it->id, &m, &why) != S_OK) {
        text_free(&post_p);
        add_report(report, it->id, why);
        return S_DAMAGED;
    }
    if (!is_job(&m)) {
        message_free(&m);
        text_free(&post_p);
        return S_NOTHING;
    }

    /* Build mini.names from the single batch scan (zero extra p_list calls!). */
    str_list link_names = {0};
    for (size_t i = 0; i < it->links.count; i++) {
        text full = {0};
        add(&full, it->id), add(&full, "."), add(&full, it->links.items[i]);
        if (full.failed || !list_push(&link_names, full.data, full.length)) link_names.failed = true;
        text_free(&full);
    }
    if (link_names.failed) {
        list_free(&link_names);
        message_free(&m);
        text_free(&post_p);
        add_report(report, it->id, "out of memory");
        return S_DAMAGED;
    }
    sort_list(&link_names);
    store mini = {.folder = folder, .names = link_names.items, .count = link_names.count};
    job j = {0};
    s_result jr = store_job(&mini, &m, &j);
    list_free(&link_names);
    if (jr == S_REFUSED) {
        message_free(&m);
        text_free(&post_p);
        it->peer_moved = true;
        return S_OK;
    }
    if (jr != S_OK) {
        message_free(&m);
        text_free(&post_p);
        add_report(report, it->id, mini.why ? mini.why : "a step of this job is damaged");
        return S_DAMAGED;
    }
    if ((j.state != DONE && j.state != REFUSED) || j.when > now || now - j.when < ARCHIVE_AGE_US) {
        message_free(&m);
        text_free(&post_p);
        return S_NOTHING;
    }

    /* Validate all live seen marks before copying anything. */
    for (size_t i = 0; i < it->seen.count; i++) {
        text sp = {0};
        live_path(folder, &sp, it->id, it->seen.items[i]);
        char *sd = NULL;
        size_t sl = 0;
        p_result sr = sp.failed ? P_FAILED : p_read(sp.data, MOST_FILE, &sd, &sl);
        text_free(&sp);
        if (sr == P_MISSING) continue;
        bool ok_seen = sr == P_OK && store_valid_seen_event(it->seen.items[i]) && sl == len(ARCHIVE_SEEN) && same(sd, ARCHIVE_SEEN);
        p_free(sd);
        if (!ok_seen) {
            message_free(&m);
            text_free(&post_p);
            add_report(report, it->id, "a seen mark is damaged");
            return S_DAMAGED;
        }
    }
    /* Validate all live landed marks before copying anything. */
    for (size_t i = 0; i < it->landed.count; i++) {
        const char *ev = it->landed.items[i];
        text lp = {0};
        live_path(folder, &lp, it->id, ev);
        char *ld = NULL;
        size_t ll = 0;
        p_result lr = lp.failed ? P_FAILED : p_read(lp.data, MOST_FILE, &ld, &ll);
        text_free(&lp);
        if (lr == P_MISSING) continue;
        bool ok_landed = lr == P_OK && store_valid_landed_event(ev) && store_valid_landed_payload(ld, ll, ev + 7);
        p_free(ld);
        if (!ok_landed) {
            message_free(&m);
            text_free(&post_p);
            add_report(report, it->id, "a landed mark is damaged");
            return S_DAMAGED;
        }
    }

    if (!ensure_archive_month(folder, it->month)) {
        message_free(&m);
        text_free(&post_p);
        add_report(report, it->id, "archive folder cannot be created");
        return S_DAMAGED;
    }

    /* Copy post, steps, and seen marks to archive/<YYYY-MM>/ before removing anything. */
    arch_at dst_post = {0};
    arch_at_of(&dst_post, folder, it->month, it->id, "post");
    if (pause) pause(pause_user, "before-copy-post");
    s_result cr = dst_post.name.failed ? (why = "out of memory", S_DAMAGED) : copy_durable(post_p.data, &dst_post, &why);
    arch_at_free(&dst_post);
    if (cr == S_DAMAGED) {
        message_free(&m);
        text_free(&post_p);
        add_report(report, it->id, why);
        return S_DAMAGED;
    }
    if (pause) pause(pause_user, "after-copy-post");

    for (unsigned k = 0; k < j.links; k++) {
        text lnm = {0}, lsrc = {0};
        arch_at ldst = {0};
        link_suffix(&lnm, k);
        if (!lnm.failed) {
            live_path(folder, &lsrc, it->id, lnm.data);
            arch_at_of(&ldst, folder, it->month, it->id, lnm.data);
        }
        if (pause) pause(pause_user, "before-copy-link");
        s_result lr = lnm.failed || lsrc.failed || ldst.name.failed ? (why = "out of memory", S_DAMAGED) : copy_durable(lsrc.data, &ldst, &why);
        text_free(&lnm), text_free(&lsrc), arch_at_free(&ldst);
        if (lr == S_DAMAGED) {
            message_free(&m);
            text_free(&post_p);
            add_report(report, it->id, why);
            return S_DAMAGED;
        }
    }
    if (pause) pause(pause_user, "after-copy-links");

    for (size_t i = 0; i < it->seen.count; i++) {
        text ssrc = {0};
        arch_at sdst = {0};
        live_path(folder, &ssrc, it->id, it->seen.items[i]);
        arch_at_of(&sdst, folder, it->month, it->id, it->seen.items[i]);
        if (pause) pause(pause_user, "before-copy-seen");
        s_result sr = ssrc.failed || sdst.name.failed ? (why = "out of memory", S_DAMAGED) : copy_durable(ssrc.data, &sdst, &why);
        text_free(&ssrc), arch_at_free(&sdst);
        if (sr == S_DAMAGED) {
            message_free(&m);
            text_free(&post_p);
            add_report(report, it->id, why);
            return S_DAMAGED;
        }
    }
    for (size_t i = 0; i < it->landed.count; i++) {
        text lsrc = {0};
        arch_at ldst = {0};
        live_path(folder, &lsrc, it->id, it->landed.items[i]);
        arch_at_of(&ldst, folder, it->month, it->id, it->landed.items[i]);
        if (pause) pause(pause_user, "before-copy-landed");
        s_result lr = lsrc.failed || ldst.name.failed ? (why = "out of memory", S_DAMAGED)
                    : copy_durable_paused(lsrc.data, &ldst, &why, pause, pause_user, "after-read-landed");
        text_free(&lsrc), arch_at_free(&ldst);
        if (lr == S_DAMAGED) {
            message_free(&m);
            text_free(&post_p);
            add_report(report, it->id, why);
            return S_DAMAGED;
        }
    }
    message_free(&m);

    if (pause) pause(pause_user, "before-remove-post");
    p_result rm_post = remove_post_exclusive(post_p.data);
    text_free(&post_p);
    if (rm_post != P_OK && rm_post != P_MISSING) {
        add_report(report, it->id, "the post could not be removed");
        return S_DAMAGED;
    }
    if (pause) pause(pause_user, "after-remove-post");

    it->arch_links = j.links;
    it->post_removed = true;
    it->did_unlink = (rm_post == P_OK);
    it->peer_moved = (rm_post == P_MISSING);
    return S_OK;
}

s_result archive_store(const char *folder, size_t limit, size_t *moved, text *report,
                       void (*pause)(void *user, const char *stage), void *pause_user) {
    if (moved) *moved = 0;
    size_t total_moved = 0, total_peer = 0, total_removed = 0;
    bool had_damage = false;
    text msg_dir = {0};
    add(&msg_dir, folder), add(&msg_dir, "/messages");
    if (msg_dir.failed) return S_DAMAGED;

    uint64_t now = p_now();
    char *cursor = NULL;
    for (;;) {
#ifdef HELIO_TEST_HOOKS
        size_t batch_cap = archive_batch_size ? archive_batch_size : 2048;
#else
        size_t batch_cap = 2048;
#endif
        batch_ctx b = {.ids = {.cap_limit = batch_cap}, .after = cursor, .truncated = false};
        p_result lr = p_list(msg_dir.data, gather_batch, &b);
        if (lr == P_MISSING) {
            list_free(&b.ids);
            p_free(cursor);
            text_free(&msg_dir);
            if (report) add(report, "there is no store here: run helio init\n");
            return S_DAMAGED;
        }
        if (lr != P_OK || b.ids.failed) {
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
        batch_events be = {.items = p_alloc(b.ids.count * sizeof *be.items), .count = b.ids.count};
        if (!be.items) {
            list_free(&b.ids);
            had_damage = true;
            break;
        }
        for (size_t i = 0; i < b.ids.count; i++) be.items[i].id = b.ids.items[i];

        /* One directory pass collects all .link.* and .seen.* for the entire batch! */
        if (!populate_batch_events(folder, &be)) {
            had_damage = true;
            if (report) add(report, "the store cannot be read\n");
        } else {
            size_t batch_post_removed = 0;
            for (size_t i = 0; i < be.count; i++) {
                s_result r = copy_and_unlink_post(folder, &be.items[i], now, report, pause, pause_user);
                if (r == S_DAMAGED) had_damage = true;
                if (be.items[i].post_removed) {
                    batch_post_removed++;
                    if (!be.items[i].peer_moved) {
                        total_moved++;
                        total_removed++;
                    } else {
                        total_peer++;
                        if (be.items[i].did_unlink) total_removed++;
                    }
                } else if (be.items[i].peer_moved) {
                    total_peer++;
                }
                if (limit && (total_moved >= limit || total_removed >= limit)) break;
            }
            /* One single post-unlink scan collects any late .seen.* marks for all moved jobs in this batch! */
            if (batch_post_removed > 0) {
                if (!populate_batch_events(folder, &be)) {
                    had_damage = true;
                    if (report) add(report, "the store cannot be read\n");
                } else {
                    for (size_t i = 0; i < be.count; i++) {
                        if (!be.items[i].post_removed) continue;
                        const char *why = NULL;
                        s_result sw = sweep_item_tail(folder, &be.items[i], pause, pause_user, &why);
                        if (sw != S_OK) {
                            add_report(report, be.items[i].id, why);
                            had_damage = true;
                        }
                    }
                }
            }
        }
        for (size_t i = 0; i < be.count; i++) {
            list_free(&be.items[i].seen);
            list_free(&be.items[i].landed);
            list_free(&be.items[i].links);
            list_free(&be.items[i].rm_parts);
        }
        p_free(be.items);

        if ((limit && (total_moved >= limit || total_removed >= limit)) || !b.truncated) {
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

s_result archive_daily(const char *folder, text *report, void (*pause)(void *user, const char *stage), void *pause_user) {
    uint64_t now = p_now();
    text day = {0}, sdir = {0}, mark = {0}, unique = {0};
    add_time(&day, now);
    if (day.data) day.data[day.length = 10] = 0;
    add(&sdir, folder), add(&sdir, "/summary");
    add(&mark, sdir.data ? sdir.data : ""), add(&mark, "/"), add(&mark, day.data ? day.data : ""), add(&mark, ".archived");
    add_number(&unique, p_tick(), 0);
    bool full = day.failed || sdir.failed || mark.failed || unique.failed;
    char *there = NULL;
    size_t tlen = 0;
    p_result got = full ? P_FAILED : p_read(mark.data, 64, &there, &tlen);
    p_free(there);
    if (got == P_OK) {
        text_free(&day), text_free(&sdir), text_free(&mark), text_free(&unique);
        return S_OK;
    }
    if (got != P_MISSING) {
        text_free(&day), text_free(&sdir), text_free(&mark), text_free(&unique);
        return S_DAMAGED;
    }
    if (p_folder(sdir.data) != P_OK) {
        text_free(&day), text_free(&sdir), text_free(&mark), text_free(&unique);
        return S_DAMAGED;
    }

    /* Partition today's 200-job budget into 10 immutable exclusive-create slots of 20 jobs each
       (summary/<YYYY-MM-DD>.slot.0 .. slot.9). Honor any legacy .claiming debit left by an interrupted
       predecessor binary on the same UTC date by skipping already-debited slots. */
    if (pause) pause(pause_user, "before-daily-claim");
    unsigned start_slot = 0;
    text legacy = {0};
    add(&legacy, sdir.data), add(&legacy, "/"), add(&legacy, day.data), add(&legacy, ".claiming");
    char *ldata = NULL;
    size_t llen = 0;
    p_result lgot = legacy.failed ? P_FAILED : p_read(legacy.data, 128, &ldata, &llen);
    text_free(&legacy);
    if (lgot == P_OK) {
        const char *p = ldata;
        while (*p >= '0' && *p <= '9') p++;
        size_t prior = 200;
        if (*p == ' ' && p[1] >= '0' && p[1] <= '9') {
            prior = 0;
            for (p++; *p >= '0' && *p <= '9' && prior < 200; p++) prior = prior * 10 + (size_t)(*p - '0');
        }
        p_free(ldata);
        start_slot = (unsigned)((prior + 19) / 20);
        if (start_slot > 10) start_slot = 10;
    } else if (lgot != P_MISSING) {
        p_free(ldata);
        text_free(&day), text_free(&sdir), text_free(&mark), text_free(&unique);
        return S_DAMAGED;
    }

    size_t spent = 0;
    s_result res = S_OK;
    bool exhausted = false;
    for (unsigned slot = start_slot; slot < 10; slot++) {
        text sp = {0}, sbody = {0};
        add(&sp, sdir.data), add(&sp, "/"), add(&sp, day.data), add(&sp, ".slot."), add_number(&sp, slot, 0);
        add_number(&sbody, p_now(), 0), add(&sbody, "\n");
        p_result sw = (sp.failed || sbody.failed) ? P_FAILED : p_create_excl(sp.data, sbody.data, sbody.length);
        text_free(&sp), text_free(&sbody);
        if (sw == P_EXISTS) continue;
        if (sw != P_OK) {
            res = S_DAMAGED;
            break;
        }
        /* This process exclusively owns slot `slot` (at most 20 jobs); run its batch and write .done.<slot>
           only on non-damaged completion so concurrent peers never publish .archived while a live slot runs. */
        size_t moved = 0;
        s_result ar = archive_store(folder, 20, &moved, report, pause, pause_user);
        spent += moved;
        if (ar == S_DAMAGED) {
            res = S_DAMAGED;
            break;
        }
        text dp = {0};
        add(&dp, sdir.data), add(&dp, "/"), add(&dp, day.data), add(&dp, ".done."), add_number(&dp, slot, 0);
        p_result dw = dp.failed ? P_FAILED : p_write(dp.data, "done\n", 5, false, unique.data);
        text_free(&dp);
        if (dw != P_OK && dw != P_EXISTS) {
            res = S_DAMAGED;
            break;
        }
        if (ar == S_NOTHING) {
            exhausted = true;
            break;
        }
    }
    if (res == S_OK) {
        /* Publish .archived only when the store is exhausted OR every slot in [start_slot..9] has either
           completed (.done.<k>) or been abandoned past the stale threshold (60s), so a concurrent inbox
           never publishes .archived while a live owner is still running slot 9. */
        bool all_finished = exhausted;
        if (!all_finished) {
            all_finished = true;
            uint64_t check_now = p_now();
            for (unsigned slot = start_slot; slot < 10 && all_finished; slot++) {
                text dp = {0};
                add(&dp, sdir.data), add(&dp, "/"), add(&dp, day.data), add(&dp, ".done."), add_number(&dp, slot, 0);
                char *dd = NULL;
                size_t dl = 0;
                p_result dr = dp.failed ? P_FAILED : p_read(dp.data, 32, &dd, &dl);
                p_free(dd);
                text_free(&dp);
                if (dr == P_OK) continue;
                if (dr != P_MISSING) {
                    res = S_DAMAGED;
                    all_finished = false;
                    break;
                }
                text sp = {0};
                add(&sp, sdir.data), add(&sp, "/"), add(&sp, day.data), add(&sp, ".slot."), add_number(&sp, slot, 0);
                char *sd = NULL;
                size_t sl = 0;
                p_result sr = sp.failed ? P_FAILED : p_read(sp.data, 64, &sd, &sl);
                if (sr == P_MISSING) {
                    p_free(sd);
                    text_free(&sp);
                    all_finished = false;
                    break;
                }
                if (sr != P_OK) {
                    p_free(sd);
                    text_free(&sp);
                    res = S_DAMAGED;
                    all_finished = false;
                    break;
                }
                uint64_t sts = 0;
                const char *p = sd;
                bool has_nl = (sl > 0 && sd[sl - 1] == '\n');
                while (*p >= '0' && *p <= '9') sts = sts * 10 + (uint64_t)(*p++ - '0');
                p_free(sd);
                if (!(has_nl && sts > 0)) {
                    /* Empty or partial slot from an interrupted create: fall back to file mtime so it expires at 60s
                       without falsely treating a brand-new slot in flight as stale. */
                    uint64_t mt = 0;
                    p_result mr = p_file_mtime(sp.data, &mt);
                    if (mr == P_OK) sts = mt;
                    else if (mr != P_MISSING) {
                        text_free(&sp);
                        res = S_DAMAGED;
                        all_finished = false;
                        break;
                    } else {
                        text_free(&sp);
                        all_finished = false;
                        break;
                    }
                }
                text_free(&sp);
                if (!(sts > 0 && check_now > sts && check_now - sts >= 60ull * 1000000ull)) all_finished = false;
            }
        }
        if (all_finished) {
            text body = {0};
            add_number(&body, (uint64_t)spent, 0), add(&body, "\n");
            p_result wr = body.failed ? P_FAILED : p_write(mark.data, body.data, body.length, false, unique.data);
            text_free(&body);
            if (wr != P_OK && wr != P_EXISTS) res = S_DAMAGED;
        }
    }
    text_free(&day), text_free(&sdir), text_free(&mark), text_free(&unique);
    return res;
}

typedef struct {
    const char *id;
    size_t id_len;
    unsigned min_link;
    bool has_higher;
} arch_gap_ctx;

static bool scan_arch_gap(void *user, const char *name) {
    arch_gap_ctx *g = user;
    size_t n = len(name);
    if (n <= g->id_len + 6) return true;
    for (size_t i = 0; i < g->id_len; i++)
        if (name[i] != g->id[i]) return true;
    if (name[g->id_len] != '.') return true;
    unsigned idx = 0;
    if (parse_link_index(name + g->id_len + 1, &idx) && idx > g->min_link) {
        g->has_higher = true;
        return false;
    }
    return true;
}

static s_result archive_read_job_ex(store *s, const message *m, const str_list *known_links, job *out) {
    *out = (job){.state = OPEN};
    char month[8];
    if (!valid_id(m->id) || !archive_month(m->id, month)) {
        s->why = "that is not a message id";
        return S_DAMAGED;
    }
    if (check_arch_dirs(s->folder, month) != S_OK) {
        s->why = "the archive folder is not a real directory";
        return S_DAMAGED;
    }
    for (; out->links < MOST_LINKS; out->links++) {
        text ev = {0};
        arch_at at = {0};
        link_suffix(&ev, out->links);
        if (!ev.failed) arch_at_of(&at, s->folder, month, m->id, ev.data);
        char *data = NULL;
        size_t length = 0;
        p_result r = ev.failed || at.name.failed ? P_FAILED : arch_read(&at, MOST_FILE, &data, &length);
        text_free(&ev), arch_at_free(&at);
        if (r == P_MISSING) {
            if (known_links) {
                if (known_links->count != out->links) {
                    s->why = "a step of this job is missing";
                    return S_DAMAGED;
                }
            } else {
                arch_gap_ctx g = {.id = m->id, .id_len = len(m->id), .min_link = out->links, .has_higher = false};
                p_result lr = arch_list(s->folder, month, scan_arch_gap, &g, NULL);
                if (lr != P_OK) {
                    s->why = "the archive cannot be read";
                    return S_DAMAGED;
                }
                if (g.has_higher) {
                    s->why = "a step of this job is missing";
                    return S_DAMAGED;
                }
            }
            if (out->state != DONE && out->state != REFUSED) {
                s->why = "an archived job is incomplete";
                return S_DAMAGED;
            }
            return S_OK;
        }
        if (r != P_OK || len(data) != length) {
            if (r == P_OK) p_free(data);
            s->why = "a step of this job cannot be read";
            return S_DAMAGED;
        }
        bool ok = store_apply_step(m, out, data);
        p_free(data);
        if (!ok) {
            s->why = "a step of this job is damaged";
            return S_DAMAGED;
        }
    }
    if (known_links && known_links->count != out->links) {
        s->why = "a step of this job is missing";
        return S_DAMAGED;
    }
    if (out->state != DONE && out->state != REFUSED) {
        s->why = "an archived job is incomplete";
        return S_DAMAGED;
    }
    return S_OK;
}

s_result archive_read_job(store *s, const message *m, job *out) {
    return archive_read_job_ex(s, m, NULL, out);
}

s_result archive_write_seen(store *s, const char *id, const char *seen_event) {
    char month[8];
    if (!valid_id(id) || !archive_month(id, month)) {
        s->why = "that is not a message id";
        return S_REFUSED;
    }
    if (check_arch_dirs(s->folder, month) != S_OK) {
        s->why = "the archive folder is not a real directory";
        return S_DAMAGED;
    }
    text unique = {0};
    arch_at at = {0};
    arch_at_of(&at, s->folder, month, id, seen_event);
    add_number(&unique, p_tick(), 0);
    p_result r = at.name.failed || unique.failed ? P_FAILED : arch_write(&at, ARCHIVE_SEEN, len(ARCHIVE_SEEN), false, unique.data, "before-arch-seen");
    text_free(&unique);
    if (r == P_OK) {
        arch_at_free(&at);
        return S_OK;
    }
    if (r == P_EXISTS) {
        char *data = NULL;
        size_t length = 0;
        p_result rr = arch_read(&at, MOST_FILE, &data, &length);
        arch_at_free(&at);
        bool ok = rr == P_OK && length == len(ARCHIVE_SEEN) && same(data, ARCHIVE_SEEN);
        p_free(data);
        if (ok) return S_OK;
        s->why = "a seen mark is damaged";
        return S_DAMAGED;
    }
    arch_at_free(&at);
    s->why = r == P_UNFLUSHED ? "the store cannot be flushed" : "the archive cannot be written";
    return S_DAMAGED;
}

static bool valid_month_name(const char *name) {
    if (len(name) != 7 || name[4] != '-') return false;
    for (int i = 0; i < 7; i++)
        if (i != 4 && (name[i] < '0' || name[i] > '9')) return false;
    int mon = (name[5] - '0') * 10 + (name[6] - '0');
    return mon >= 1 && mon <= 12;
}

typedef struct {
    str_list months;
    str_list bad;
    bool failed;
} month_list_ctx;

static bool gather_months(void *user, const char *name) {
    month_list_ctx *m = user;
    if (valid_month_name(name)) {
        if (!list_push(&m->months, name, len(name))) return m->failed = true, false;
    } else {
        if (!list_push(&m->bad, name, len(name))) return m->failed = true, false;
    }
    return true;
}

static bool was_emitted(char *const *emitted, size_t count, const char *id) {
    for (size_t i = 0; i < count; i++)
        if (same(emitted[i], id)) return true;
    return false;
}

static bool gather_arch_post_batch(void *user, const char *name) {
    batch_ctx *b = user;
    size_t n = len(name);
    if (n <= 5 || !same(name + n - 5, ".post")) return true;
    size_t id_len = n - 5;
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

static bool attach_arch_events(void *user, const char *name);

s_result archive_read_landed_one(const char *folder, const char *id, const char *commit) {
    char month[8];
    if (!valid_id(id) || !archive_month(id, month)) return S_NOTHING;
    text ev = {0};
    arch_at ap = {0};
    add(&ev, "landed."), add(&ev, commit);
    if (ev.failed || !store_valid_landed_event(ev.data)) {
        text_free(&ev);
        return S_DAMAGED;
    }
    arch_at_of(&ap, folder, month, id, ev.data);
    text_free(&ev);
    char *data = NULL;
    size_t length = 0;
    p_result r = ap.name.failed ? P_FAILED : arch_read(&ap, MOST_FILE, &data, &length);
    arch_at_free(&ap);
    if (r == P_MISSING) return S_NOTHING;
    if (r != P_OK) return S_DAMAGED;
    bool ok = store_valid_landed_payload(data, length, commit);
    p_free(data);
    return ok ? S_OK : S_DAMAGED;
}

s_result archive_promote_landed(const char *folder, const char *id, const char *commit, const char **why) {
    char month[8];
    if (!valid_id(id) || !archive_month(id, month)) {
        if (why) *why = "the message id has no valid date";
        return S_DAMAGED;
    }
    if (!ensure_archive_month(folder, month)) {
        if (why) *why = "archive folder cannot be created";
        return S_DAMAGED;
    }
    text ev = {0}, src = {0};
    arch_at dst = {0};
    add(&ev, "landed."), add(&ev, commit);
    if (ev.failed || !store_valid_landed_event(ev.data)) {
        text_free(&ev);
        if (why) *why = "a landed mark is damaged";
        return S_DAMAGED;
    }
    live_path(folder, &src, id, ev.data);
    arch_at_of(&dst, folder, month, id, ev.data);
    text_free(&ev);
    const char *cwhy = NULL;
    s_result cr = (src.failed || dst.name.failed) ? (cwhy = "out of memory", S_DAMAGED) : copy_durable_ex(src.data, &dst, &cwhy, NULL, NULL, NULL, NULL, "before-landed-promote");
    if (cr == S_DAMAGED) {
        text_free(&src), arch_at_free(&dst);
        if (why) *why = cwhy ? cwhy : "an archive copy cannot be written";
        return S_DAMAGED;
    }
    p_result rm = p_remove(src.data);
    text_free(&src), arch_at_free(&dst);
    if (rm != P_OK && rm != P_MISSING) {
        if (why) *why = "a landed mark could not be removed";
        return S_DAMAGED;
    }
    return S_OK;
}

typedef struct {
    const char *prefix;
    size_t prefix_len;
    const char *bare;
    str_list events;
    bool bad;
} arch_landed_ctx;

static bool gather_arch_landed(void *user, const char *name) {
    arch_landed_ctx *c = user;
    if (same(name, c->bare)) {
        c->bad = true;
        return true;
    }
    if (!starts(name, c->prefix)) return true;
    size_t n = len(name);
    if (n > 5 && same(name + n - 5, ".part")) return true;
    const char *ev = name + (c->prefix_len - 7); /* "landed.<commit>" */
    if (!store_valid_landed_event(ev)) {
        c->bad = true;
        return true;
    }
    return list_push(&c->events, ev, len(ev));
}

static s_result format_arch_landed_events(const store *s, const char *month, const char *id,
                                          str_list *events, text *out, const char **why) {
    /* Also include any un-swept live <id>.landed.* marks from s->names (interrupted archive recovery). */
    text prefix = {0}, bare = {0};
    add(&prefix, id), add(&prefix, ".landed.");
    add(&bare, id), add(&bare, ".landed");
    if (prefix.failed || bare.failed) {
        text_free(&prefix), text_free(&bare);
        if (why) *why = "out of memory";
        return S_DAMAGED;
    }
    str_list live_extra = {0};
    size_t low = 0, high = s->count;
    while (low < high) {
        size_t mid = low + (high - low) / 2;
        if (compare(s->names[mid], bare.data) < 0) low = mid + 1;
        else high = mid;
    }
    for (size_t i = low; i < s->count; i++) {
        if (same(s->names[i], bare.data)) {
            list_free(&live_extra), text_free(&prefix), text_free(&bare);
            if (why) *why = "a landed mark is damaged";
            return S_DAMAGED;
        }
        if (!starts(s->names[i], prefix.data)) {
            if (compare(s->names[i], prefix.data) > 0) break;
            continue;
        }
        const char *ev = s->names[i] + len(id) + 1;
        if (!store_valid_landed_event(ev)) {
            list_free(&live_extra), text_free(&prefix), text_free(&bare);
            if (why) *why = "a landed mark is damaged";
            return S_DAMAGED;
        }
        text lp = {0};
        live_path(s->folder, &lp, id, ev);
        char *ld = NULL;
        size_t ll = 0;
        p_result lr = lp.failed ? P_FAILED : p_read(lp.data, MOST_FILE, &ld, &ll);
        text_free(&lp);
        if (lr == P_MISSING) {
            s_result al = archive_read_landed_one(s->folder, id, ev + 7);
            if (al == S_OK) {
                if (!list_push(&live_extra, ev, len(ev))) {
                    list_free(&live_extra), text_free(&prefix), text_free(&bare);
                    if (why) *why = "out of memory";
                    return S_DAMAGED;
                }
            } else if (al == S_DAMAGED) {
                list_free(&live_extra), text_free(&prefix), text_free(&bare);
                if (why) *why = "an archived landed mark is damaged";
                return S_DAMAGED;
            }
            continue;
        }
        bool ok = lr == P_OK && store_valid_landed_payload(ld, ll, ev + 7);
        p_free(ld);
        if (!ok || !list_push(&live_extra, ev, len(ev))) {
            list_free(&live_extra), text_free(&prefix), text_free(&bare);
            if (why) *why = ok ? "out of memory" : "a landed mark is damaged";
            return S_DAMAGED;
        }
    }
    text_free(&prefix), text_free(&bare);

    sort_list(events);
    sort_list(&live_extra);
    size_t ai = 0, li = 0, shown = 0;
    while (ai < events->count || li < live_extra.count) {
        const char *ev = NULL;
        bool from_arch = false;
        if (ai < events->count && li < live_extra.count) {
            int cmp = compare(events->items[ai], live_extra.items[li]);
            if (cmp < 0) ev = events->items[ai++], from_arch = true;
            else if (cmp > 0) ev = live_extra.items[li++];
            else ev = events->items[ai++], li++, from_arch = true;
        } else if (ai < events->count) {
            ev = events->items[ai++], from_arch = true;
        } else {
            ev = live_extra.items[li++];
        }
        if (from_arch) {
            if (!store_valid_landed_event(ev)) {
                list_free(&live_extra);
                if (why) *why = "a landed mark is damaged";
                return S_DAMAGED;
            }
            arch_at ap = {0};
            arch_at_of(&ap, s->folder, month, id, ev);
            char *data = NULL;
            size_t length = 0;
            p_result r = ap.name.failed ? P_FAILED : arch_read(&ap, MOST_FILE, &data, &length);
            arch_at_free(&ap);
            bool ok = r == P_OK && store_valid_landed_payload(data, length, ev + 7);
            p_free(data);
            if (!ok) {
                list_free(&live_extra);
                if (why) *why = "a landed mark is damaged";
                return S_DAMAGED;
            }
        }
        if (shown) add(out, ",");
        add(out, ev + 7);
        shown++;
    }
    list_free(&live_extra);
    if (out->failed) {
        if (why) *why = "out of memory";
        return S_DAMAGED;
    }
    return S_OK;
}

s_result archive_each_landed(const char *folder, const char *id, bool (*each)(void *user, const char *commit), void *user, const char **why) {
    char month[8];
    if (!valid_id(id) || !archive_month(id, month)) return S_OK;
    text prefix = {0}, bare = {0};
    add(&prefix, id), add(&prefix, ".landed.");
    add(&bare, id), add(&bare, ".landed");
    if (prefix.failed || bare.failed) {
        text_free(&prefix), text_free(&bare);
        if (why) *why = "out of memory";
        return S_DAMAGED;
    }
    arch_landed_ctx ctx = {.prefix = prefix.data, .prefix_len = prefix.length, .bare = bare.data};
    p_result lr = arch_list(folder, month, gather_arch_landed, &ctx, NULL);
    text_free(&prefix), text_free(&bare);
    if (lr == P_MISSING) {
        list_free(&ctx.events);
        return S_OK;
    }
    if (lr != P_OK || ctx.events.failed || ctx.bad) {
        list_free(&ctx.events);
        if (why) *why = ctx.bad ? "a landed mark is damaged" : "the archive month cannot be read";
        return S_DAMAGED;
    }
    sort_list(&ctx.events);
    for (size_t i = 0; i < ctx.events.count; i++) {
        const char *ev = ctx.events.items[i];
        arch_at ap = {0};
        arch_at_of(&ap, folder, month, id, ev);
        char *data = NULL;
        size_t length = 0;
        p_result r = ap.name.failed ? P_FAILED : arch_read(&ap, MOST_FILE, &data, &length);
        arch_at_free(&ap);
        bool ok = r == P_OK && store_valid_landed_payload(data, length, ev + 7);
        p_free(data);
        if (!ok) {
            list_free(&ctx.events);
            if (why) *why = "a landed mark is damaged";
            return S_DAMAGED;
        }
        if (!each(user, ev + 7)) {
            list_free(&ctx.events);
            if (why) *why = "out of memory";
            return S_DAMAGED;
        }
    }
    list_free(&ctx.events);
    return S_OK;
}

size_t archive_every(store *s, char *const *emitted, size_t emitted_count,
                     bool (*visible)(const message *m, void *user),
                     bool (*each)(store *s, const message *m, const job *known, const char *landed, void *user), void *user) {
    month_list_ctx ml = {0};
    p_result r = arch_list(s->folder, NULL, gather_months, &ml, "arch-root-held");
    if (r == P_MISSING) {
        list_free(&ml.months), list_free(&ml.bad);
        return 0;
    }
    if (r != P_OK || ml.failed) {
        list_free(&ml.months), list_free(&ml.bad);
        return 1;
    }
    size_t damaged = ml.bad.count;
    sort_list(&ml.months);
    /* Iterate newest month first (descending YYYY-MM). */
    for (size_t mi = ml.months.count; mi-- > 0;) {
        const char *month = ml.months.items[mi];
        if (check_arch_dirs(s->folder, month) != S_OK) {
            damaged++;
            continue;
        }
        char *cursor = NULL;
        for (;;) {
#ifdef HELIO_TEST_HOOKS
            size_t batch_cap = archive_batch_size ? archive_batch_size : 2048;
#else
            size_t batch_cap = 2048;
#endif
            batch_ctx b = {.ids = {.cap_limit = batch_cap}, .after = cursor, .truncated = false};
            p_result lr = arch_list(s->folder, month, gather_arch_post_batch, &b, NULL);
            if (lr != P_OK || b.ids.failed) {
                damaged++;
                list_free(&b.ids);
                break;
            }
            if (b.ids.count == 0) {
                list_free(&b.ids);
                break;
            }
            sort_list(&b.ids);
            /* Single batch scan collects all .link.* for every job in this batch (O(N), zero per-job p_list!). */
            batch_events be = {.items = p_alloc(b.ids.count * sizeof *be.items), .count = b.ids.count};
            if (!be.items) {
                damaged++;
            } else {
                for (size_t i = 0; i < b.ids.count; i++) be.items[i].id = b.ids.items[i];
                if (arch_list(s->folder, month, attach_arch_events, &be, NULL) != P_OK || be.failed) {
                    damaged++;
                } else {
                    bool keep_going = true;
                    for (size_t i = 0; keep_going && i < be.count; i++) {
                        const char *id = be.items[i].id;
                        char derived[8];
                        if (!valid_id(id) || !archive_month(id, derived) || !same(derived, month)) {
                            damaged++;
                            continue;
                        }
                        if (was_emitted(emitted, emitted_count, id)) continue;
                        message m = {0};
                        s_result mr = archive_read_message(s, id, &m);
                        if (mr == S_OK) {
                            if (visible && !visible(&m, user)) {
                                message_free(&m);
                                continue;
                            }
                            if (is_job(&m)) {
                                job known = {0};
                                s_result jr = archive_read_job_ex(s, &m, &be.items[i].links, &known);
                                text landed_t = {0};
                                const char *lwhy = NULL;
                                s_result lr2 = jr == S_OK ? format_arch_landed_events(s, month, id, &be.items[i].landed, &landed_t, &lwhy) : S_OK;
                                if (jr == S_OK) {
                                    if (lr2 != S_OK) damaged++;
                                    keep_going = each(s, &m, &known, lr2 == S_OK ? landed_t.data : NULL, user);
                                } else {
                                    keep_going = each(s, &m, NULL, NULL, user);
                                    damaged++;
                                }
                                text_free(&landed_t);
                            } else {
                                keep_going = each(s, &m, NULL, NULL, user);
                            }
                        } else if (mr != S_NOTHING) {
                            damaged++;
                        }
                        message_free(&m);
                    }
                    if (!keep_going) b.truncated = false;
                }
                for (size_t i = 0; i < be.count; i++) {
                    list_free(&be.items[i].seen);
                    list_free(&be.items[i].landed);
                    list_free(&be.items[i].links);
                    list_free(&be.items[i].rm_parts);
                }
                p_free(be.items);
            }
            if (!b.truncated) {
                list_free(&b.ids);
                break;
            }
            char *next = copy(b.ids.items[b.ids.count - 1], len(b.ids.items[b.ids.count - 1]));
            list_free(&b.ids);
            p_free(cursor);
            cursor = next;
            if (!cursor) {
                damaged++;
                break;
            }
        }
        p_free(cursor);
    }
    list_free(&ml.months), list_free(&ml.bad);
    return damaged;
}

typedef struct {
    const char *month;
    text *report;
    batch_ctx ids;
    bool had_bad;
    bool check_bad;
} arch_check_ctx;

static bool valid_arch_event(const char *ev) {
    if (same(ev, "post")) return true;
    if (starts(ev, "seen.")) return store_valid_seen_event(ev);
    if (starts(ev, "landed.")) return store_valid_landed_event(ev);
    unsigned idx = 0;
    return len(ev) == 9 && starts(ev, "link.") && parse_link_index(ev, &idx);
}

static void report_arch(text *report, const char *month, const char *name, const char *why) {
    if (!report) return;
    text clean_m = {0}, clean_n = {0};
    clean(&clean_m, month, true);
    clean(&clean_n, name, true);
    add(report, "archive/"), add(report, clean_m.data ? clean_m.data : "?");
    if (name && *name) add(report, "/"), add(report, clean_n.data ? clean_n.data : "?");
    add(report, ": "), add(report, why ? why : "damaged"), add(report, "\n");
    text_free(&clean_m), text_free(&clean_n);
}

static bool gather_arch_check_batch(void *user, const char *name) {
    arch_check_ctx *c = user;
    size_t n = len(name);
    if (n > 5 && same(name + n - 5, ".part")) return true;
    const char *dot = name;
    while (*dot && *dot != '.') dot++;
    if (!*dot) {
        if (c->check_bad) {
            report_arch(c->report, c->month, name, "unexpected file in archive");
            c->had_bad = true;
        }
        return true;
    }
    size_t id_len = (size_t)(dot - name);
    char *id = copy(name, id_len);
    if (!id) return c->ids.ids.failed = true, false;
    if (!valid_id(id) || !valid_arch_event(dot + 1)) {
        if (c->check_bad) {
            report_arch(c->report, c->month, name, !valid_id(id) ? "that is not a message id" : "unexpected file in archive");
            c->had_bad = true;
        }
        p_free(id);
        return true;
    }
    if (c->ids.after && compare(id, c->ids.after) <= 0) {
        p_free(id);
        return true;
    }
    if (!list_contains(&c->ids.ids, id, id_len)) {
        if (c->ids.ids.count < c->ids.ids.cap_limit) {
            if (!list_push(&c->ids.ids, id, id_len)) {
                p_free(id);
                return false;
            }
        } else {
            c->ids.truncated = true;
            size_t max_i = 0;
            for (size_t i = 1; i < c->ids.ids.count; i++)
                if (compare(c->ids.ids.items[i], c->ids.ids.items[max_i]) > 0) max_i = i;
            if (compare(id, c->ids.ids.items[max_i]) < 0) {
                p_free(c->ids.ids.items[max_i]);
                c->ids.ids.items[max_i] = id;
                return true;
            }
        }
    }
    p_free(id);
    return true;
}

static bool attach_arch_events(void *user, const char *name) {
    batch_events *be = user;
    size_t n = len(name);
    if (n > 5 && same(name + n - 5, ".part")) return true;
    const char *dot = name;
    while (*dot && *dot != '.') dot++;
    if (!*dot) return true;
    size_t id_len = (size_t)(dot - name);
    int idx = find_batch_id(be, name, id_len);
    if (idx < 0) return true;
    const char *ev = dot + 1;
    if (same(ev, "post")) be->items[idx].post_removed = true; /* reuse flag to record archived .post presence */
    else if (starts(ev, "seen.")) list_push(&be->items[idx].seen, ev, len(ev));
    else if (same(ev, "landed") || starts(ev, "landed.")) list_push(&be->items[idx].landed, ev, len(ev));
    else if (starts(ev, "link.")) list_push(&be->items[idx].links, ev, len(ev));
    if (be->items[idx].seen.failed || be->items[idx].landed.failed || be->items[idx].links.failed) return be->failed = true, false;
    return true;
}

/* Returns S_OK if equal, S_NOTHING if live file b vanished concurrently, S_DAMAGED if mismatched or unreadable. */
static s_result compare_arch_live(const arch_at *a, const char *b) {
    char *da = NULL, *db = NULL;
    size_t la = 0, lb = 0;
    p_result ra = arch_read(a, MOST_FILE, &da, &la);
    p_result rb = p_read(b, MOST_FILE, &db, &lb);
    if (rb == P_MISSING) {
        p_free(da), p_free(db);
        return S_NOTHING;
    }
    bool eq = ra == P_OK && rb == P_OK && la == lb;
    for (size_t i = 0; eq && i < la; i++) eq = da[i] == db[i];
    p_free(da), p_free(db);
    return eq ? S_OK : S_DAMAGED;
}

static void report_arch_file(text *report, const char *month, const char *id, const char *ev, const char *why) {
    text full = {0};
    add(&full, id), add(&full, "."), add(&full, ev);
    report_arch(report, month, full.failed ? id : full.data, why);
    text_free(&full);
}

s_result archive_check(store *s, text *report) {
    month_list_ctx ml = {0};
    p_result r = arch_list(s->folder, NULL, gather_months, &ml, "arch-root-held");
    if (r == P_MISSING) {
        list_free(&ml.months), list_free(&ml.bad);
        return S_OK;
    }
    if (r != P_OK || ml.failed) {
        add_report(report, "archive", "the archive cannot be read");
        list_free(&ml.months), list_free(&ml.bad);
        return S_DAMAGED;
    }
    s_result all = S_OK;
    for (size_t i = 0; i < ml.bad.count; i++) {
        report_arch(report, ml.bad.items[i], "", "unexpected entry in archive");
        all = S_DAMAGED;
    }
    sort_list(&ml.months);
    for (size_t mi = 0; mi < ml.months.count; mi++) {
        const char *month = ml.months.items[mi];
        if (check_arch_dirs(s->folder, month) != S_OK) {
            report_arch(report, month, "", "archive month is not a real directory");
            all = S_DAMAGED;
            continue;
        }
        char *cursor = NULL;
        bool first_pass = true;
        for (;;) {
#ifdef HELIO_TEST_HOOKS
            size_t batch_cap = archive_batch_size ? archive_batch_size : 2048;
#else
            size_t batch_cap = 2048;
#endif
            arch_check_ctx ac = {
                .month = month,
                .report = report,
                .ids = {.ids = {.cap_limit = batch_cap}, .after = cursor, .truncated = false},
                .had_bad = false,
                .check_bad = first_pass,
            };
            first_pass = false;
            p_result lr = arch_list(s->folder, month, gather_arch_check_batch, &ac, NULL);
            if (ac.had_bad) all = S_DAMAGED;
            if (lr != P_OK || ac.ids.ids.failed) {
                report_arch(report, month, "", "the archive month cannot be read");
                all = S_DAMAGED;
                list_free(&ac.ids.ids);
                break;
            }
            if (ac.ids.ids.count == 0) {
                list_free(&ac.ids.ids);
                break;
            }
            sort_list(&ac.ids.ids);
            batch_events be = {.items = p_alloc(ac.ids.ids.count * sizeof *be.items), .count = ac.ids.ids.count};
            if (!be.items) {
                list_free(&ac.ids.ids);
                all = S_DAMAGED;
                break;
            }
            for (size_t i = 0; i < ac.ids.ids.count; i++) be.items[i].id = ac.ids.ids.items[i];
            if (arch_list(s->folder, month, attach_arch_events, &be, NULL) != P_OK || be.failed) {
                report_arch(report, month, "", "the archive month cannot be read");
                all = S_DAMAGED;
            } else {
                for (size_t i = 0; i < be.count; i++) {
                    batch_item *it = &be.items[i];
                    sort_list(&it->links);
                    sort_list(&it->seen);
                    sort_list(&it->landed);
                    char derived[8];
                    bool month_ok = archive_month(it->id, derived) && same(derived, month);
                    if (!month_ok) {
                        report_arch(report, month, it->id, "archived message is in the wrong month folder");
                        all = S_DAMAGED;
                        continue;
                    }
                    text lpost = {0};
                    live_path(s->folder, &lpost, it->id, "post");
                    char *lprobe = NULL;
                    size_t lplen = 0;
                    p_result lpr = lpost.failed ? P_FAILED : p_read(lpost.data, MOST_FILE, &lprobe, &lplen);
                    p_free(lprobe);
                    text_free(&lpost);
                    if (lpr == P_OK) {
                        /* Interrupted move in progress: every archived file must match its live counterpart byte-for-byte.
                           If a live file disappears concurrently as the archiver completes, fall through to archive-only check. */
                        bool vanished = false;
                        if (it->post_removed) {
                            text lp = {0};
                            arch_at ap = {0};
                            arch_at_of(&ap, s->folder, month, it->id, "post");
                            live_path(s->folder, &lp, it->id, "post");
                            s_result eq = ap.name.failed || lp.failed ? S_DAMAGED : compare_arch_live(&ap, lp.data);
                            arch_at_free(&ap), text_free(&lp);
                            if (eq == S_NOTHING) vanished = true;
                            else if (eq != S_OK) {
                                report_arch_file(report, month, it->id, "post", "archived post conflicts with live post");
                                all = S_DAMAGED;
                            }
                        }
                        for (size_t k = 0; !vanished && k < it->links.count; k++) {
                            text lp = {0};
                            arch_at ap = {0};
                            arch_at_of(&ap, s->folder, month, it->id, it->links.items[k]);
                            live_path(s->folder, &lp, it->id, it->links.items[k]);
                            s_result eq = ap.name.failed || lp.failed ? S_DAMAGED : compare_arch_live(&ap, lp.data);
                            arch_at_free(&ap), text_free(&lp);
                            if (eq == S_NOTHING) vanished = true;
                            else if (eq != S_OK) {
                                report_arch_file(report, month, it->id, it->links.items[k], "archived step conflicts with live step");
                                all = S_DAMAGED;
                            }
                        }
                        for (size_t k = 0; !vanished && k < it->seen.count; k++) {
                            text lp = {0};
                            arch_at ap = {0};
                            arch_at_of(&ap, s->folder, month, it->id, it->seen.items[k]);
                            live_path(s->folder, &lp, it->id, it->seen.items[k]);
                            s_result eq = ap.name.failed || lp.failed ? S_DAMAGED : compare_arch_live(&ap, lp.data);
                            arch_at_free(&ap), text_free(&lp);
                            if (eq == S_NOTHING) vanished = true;
                            else if (eq != S_OK) {
                                report_arch_file(report, month, it->id, it->seen.items[k], "archived seen mark conflicts with live seen mark");
                                all = S_DAMAGED;
                            }
                        }
                        for (size_t k = 0; !vanished && k < it->landed.count; k++) {
                            text lp = {0};
                            arch_at ap = {0};
                            arch_at_of(&ap, s->folder, month, it->id, it->landed.items[k]);
                            live_path(s->folder, &lp, it->id, it->landed.items[k]);
                            s_result eq = ap.name.failed || lp.failed ? S_DAMAGED : compare_arch_live(&ap, lp.data);
                            arch_at_free(&ap), text_free(&lp);
                            if (eq == S_NOTHING) vanished = true;
                            else if (eq != S_OK) {
                                report_arch_file(report, month, it->id, it->landed.items[k], "archived landed mark conflicts with live landed mark");
                                all = S_DAMAGED;
                            }
                        }
                        if (!vanished) continue;
                    }
                    if (lpr != P_MISSING && lpr != P_OK) {
                        report_arch(report, month, it->id, "the live message cannot be read");
                        all = S_DAMAGED;
                        continue;
                    }
                    /* Archive-only message: must have valid .post, complete terminal job chain (if job), and valid seen marks. */
                    if (!it->post_removed) {
                        report_arch(report, month, it->id, "archived events without post");
                        all = S_DAMAGED;
                        continue;
                    }
                    message m = {0};
                    s_result mr = archive_read_message(s, it->id, &m);
                    if (mr != S_OK) {
                        report_arch_file(report, month, it->id, "post", s->why ? s->why : "archived message is damaged");
                        all = S_DAMAGED;
                        message_free(&m);
                        continue;
                    }
                    if (is_job(&m)) {
                        job now = {0};
                        s_result jr = archive_read_job_ex(s, &m, &it->links, &now);
                        if (jr != S_OK) {
                            report_arch(report, month, it->id, s->why ? s->why : "archived job is damaged");
                            all = S_DAMAGED;
                        }
                    } else if (it->links.count > 0 || it->landed.count > 0) {
                        report_arch(report, month, it->id, it->links.count > 0 ? "non-job archived message has step files" : "non-job archived message has landed marks");
                        all = S_DAMAGED;
                    }
                    bool was_job = is_job(&m);
                    message_free(&m);
                    for (size_t k = 0; k < it->seen.count; k++) {
                        arch_at ap = {0};
                        arch_at_of(&ap, s->folder, month, it->id, it->seen.items[k]);
                        char *sd = NULL;
                        size_t sl = 0;
                        p_result sr = ap.name.failed ? P_FAILED : arch_read(&ap, MOST_FILE, &sd, &sl);
                        arch_at_free(&ap);
                        bool ok_seen = sr == P_OK && store_valid_seen_event(it->seen.items[k]) && sl == len(ARCHIVE_SEEN) && same(sd, ARCHIVE_SEEN);
                        p_free(sd);
                        if (!ok_seen) {
                            report_arch_file(report, month, it->id, it->seen.items[k], "a seen mark is damaged");
                            all = S_DAMAGED;
                        }
                    }
                    if (was_job) {
                        for (size_t k = 0; k < it->landed.count; k++) {
                            const char *ev = it->landed.items[k];
                            arch_at ap = {0};
                            arch_at_of(&ap, s->folder, month, it->id, ev);
                            char *ld = NULL;
                            size_t ll = 0;
                            p_result lr = ap.name.failed ? P_FAILED : arch_read(&ap, MOST_FILE, &ld, &ll);
                            arch_at_free(&ap);
                            bool ok_landed = lr == P_OK && store_valid_landed_event(ev) && store_valid_landed_payload(ld, ll, ev + 7);
                            p_free(ld);
                            if (!ok_landed) {
                                report_arch_file(report, month, it->id, ev, "a landed mark is damaged");
                                all = S_DAMAGED;
                            }
                        }
                    }
                }
            }
            for (size_t i = 0; i < be.count; i++) {
                list_free(&be.items[i].seen);
                list_free(&be.items[i].landed);
                list_free(&be.items[i].links);
                list_free(&be.items[i].rm_parts);
            }
            p_free(be.items);
            if (!ac.ids.truncated) {
                list_free(&ac.ids.ids);
                break;
            }
            char *next = copy(ac.ids.ids.items[ac.ids.ids.count - 1], len(ac.ids.ids.items[ac.ids.ids.count - 1]));
            list_free(&ac.ids.ids);
            p_free(cursor);
            cursor = next;
            if (!cursor) {
                all = S_DAMAGED;
                break;
            }
        }
        p_free(cursor);
    }
    list_free(&ml.months), list_free(&ml.bad);
    return all;
}
