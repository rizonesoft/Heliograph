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

static void arch_dir(const char *folder, const char *month, text *out) {
    add(out, folder), add(out, "/archive/"), add(out, month);
}

static void arch_path(const char *folder, const char *month, text *out, const char *id, const char *event) {
    arch_dir(folder, month, out), add(out, "/"), add(out, id), add(out, "."), add(out, event);
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
    return store_parse_post(data, length, id, out, &s->why);
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
    if (starts(event, "seen.")) return length == len(ARCHIVE_SEEN) && same(data, ARCHIVE_SEEN);
    unsigned idx = 0;
    if (!parse_link_index(event, &idx)) return false;
    if (idx >= arch_links) return true; /* late step from a writer that raced post removal */
    char month[8];
    if (!archive_month(id, month)) return false;
    text dst = {0};
    arch_path(folder, month, &dst, id, event);
    char *adata = NULL;
    size_t alen = 0;
    p_result r = dst.failed ? P_FAILED : p_read(dst.data, MOST_FILE, &adata, &alen);
    text_free(&dst);
    bool equal = r == P_OK && alen == length;
    for (size_t i = 0; equal && i < length; i++) equal = adata[i] == data[i];
    p_free(adata);
    return equal;
}

static s_result copy_durable_ex(const char *src, const char *dst, const char **why, bool *created) {
    if (created) *created = false;
    char *data = NULL;
    size_t length = 0;
    p_result r = p_read(src, MOST_FILE, &data, &length);
    if (r == P_MISSING) {
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
        if (p_sync(dst) != P_OK) {
            *why = "an archive copy cannot be flushed";
            return S_DAMAGED;
        }
        if (created) *created = true;
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

static s_result copy_durable(const char *src, const char *dst, const char **why) {
    return copy_durable_ex(src, dst, why, NULL);
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
    else if (starts(ev, "link.")) list_push(&be->items[idx].links, ev, len(ev));
    else if (is_post_part_ev(ev, len(ev))) list_push(&be->items[idx].rm_parts, ev, len(ev));
    if (be->items[idx].seen.failed || be->items[idx].links.failed || be->items[idx].rm_parts.failed) return be->failed = true, false;
    return true;
}

static bool populate_batch_events(const char *folder, batch_events *be) {
    for (size_t i = 0; i < be->count; i++) {
        list_free(&be->items[i].seen);
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

static bool ensure_archive_month(const char *folder, const char *month) {
    text a = {0}, m = {0};
    add(&a, folder), add(&a, "/archive");
    arch_dir(folder, month, &m);
    bool ok = !a.failed && !m.failed && p_folder(a.data) == P_OK && p_folder(m.data) == P_OK;
    text_free(&a), text_free(&m);
    return ok;
}

/* Verifies and removes live .seen.* (copying valid late marks first) and live .link.* (last-to-first,
   verifying byte-equality against archive copies before removing). */
static s_result sweep_item_tail(const char *folder, const batch_item *it,
                                void (*pause)(void *user, const char *stage), void *pause_user, const char **why) {
    s_result res = S_OK;
    for (size_t i = 0; res == S_OK && i < it->seen.count; i++) {
        text src = {0}, dst = {0};
        live_path(folder, &src, it->id, it->seen.items[i]);
        arch_path(folder, it->month, &dst, it->id, it->seen.items[i]);
        char *data = NULL;
        size_t length = 0;
        p_result sr = src.failed || dst.failed ? P_FAILED : p_read(src.data, MOST_FILE, &data, &length);
        if (sr == P_OK) {
            bool valid_seen = length == len(ARCHIVE_SEEN) && same(data, ARCHIVE_SEEN);
            p_free(data);
            if (!valid_seen) {
                *why = "a seen mark is damaged";
                res = S_DAMAGED;
            } else {
                s_result cr = copy_durable(src.data, dst.data, why);
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
        text_free(&src), text_free(&dst);
    }
    if (res == S_OK && pause) pause(pause_user, "after-remove-seen");
    for (size_t i = it->links.count; res == S_OK && i-- > 0;) {
        const char *ev = it->links.items[i];
        text src = {0}, dst = {0};
        live_path(folder, &src, it->id, ev);
        arch_path(folder, it->month, &dst, it->id, ev);
        char *ldata = NULL;
        size_t llen = 0;
        p_result lr = src.failed || dst.failed ? P_FAILED : p_read(src.data, MOST_FILE, &ldata, &llen);
        if (lr == P_MISSING) {
            text_free(&src), text_free(&dst);
            continue;
        }
        if (lr != P_OK) {
            *why = "a live step cannot be read";
            res = S_DAMAGED;
            text_free(&src), text_free(&dst);
            break;
        }
        unsigned idx = 0;
        bool known_idx = parse_link_index(ev, &idx);
        if (!known_idx) {
            p_free(ldata);
            *why = "a step name is damaged";
            res = S_DAMAGED;
            text_free(&src), text_free(&dst);
            break;
        }
        if (idx < it->arch_links) {
            char *adata = NULL;
            size_t alen = 0;
            p_result ar = p_read(dst.data, MOST_FILE, &adata, &alen);
            bool equal = ar == P_OK && alen == llen;
            for (size_t k = 0; equal && k < llen; k++) equal = adata[k] == ldata[k];
            p_free(adata);
            if (!equal) {
                p_free(ldata);
                *why = "archive copy differs from live step";
                res = S_DAMAGED;
                text_free(&src), text_free(&dst);
                break;
            }
            if (p_sync(dst.data) != P_OK) {
                p_free(ldata);
                *why = "an archive copy cannot be flushed";
                res = S_DAMAGED;
                text_free(&src), text_free(&dst);
                break;
            }
        }
        p_free(ldata);
        if (pause) pause(pause_user, "before-remove-link");
        p_result rm = p_remove(src.data);
        text_free(&src), text_free(&dst);
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
            if (tr == S_DAMAGED || it->links.count > 0 || it->seen.count > 0 || has_rm_part) {
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
        text dst_post = {0};
        arch_path(folder, it->month, &dst_post, it->id, "post");
        char *adata = NULL;
        size_t alen = 0;
        p_result ar = dst_post.failed ? P_FAILED : p_read(dst_post.data, MOST_FILE, &adata, &alen);
        bool equal = ar == P_OK && alen == plen;
        for (size_t k = 0; equal && k < plen; k++) equal = adata[k] == pdata[k];
        p_free(adata);
        p_free(pdata);
        if (!equal) {
            text_free(&dst_post), text_free(&post_p);
            add_report(report, it->id, "archive copy differs from live file");
            return S_DAMAGED;
        }
        if (p_sync(dst_post.data) != P_OK) {
            text_free(&dst_post), text_free(&post_p);
            add_report(report, it->id, "an archive copy cannot be flushed");
            return S_DAMAGED;
        }
        text_free(&dst_post);
        /* Verify, copy, and flush every remaining live step BEFORE unlinking the live post. */
        for (size_t i = 0; i < it->links.count; i++) {
            text src = {0}, dst = {0};
            live_path(folder, &src, it->id, it->links.items[i]);
            arch_path(folder, it->month, &dst, it->id, it->links.items[i]);
            s_result cr = (src.failed || dst.failed) ? S_DAMAGED : copy_durable(src.data, dst.data, &why);
            text_free(&src), text_free(&dst);
            if (cr == S_DAMAGED) {
                text_free(&post_p);
                add_report(report, it->id, why ? why : "an archive step cannot be preserved");
                return S_DAMAGED;
            }
        }
        /* Validate, copy, and flush every remaining live seen mark BEFORE unlinking the live post. */
        for (size_t i = 0; i < it->seen.count; i++) {
            text src = {0}, dst = {0};
            live_path(folder, &src, it->id, it->seen.items[i]);
            char *sd = NULL;
            size_t sl = 0;
            p_result sr = src.failed ? P_FAILED : p_read(src.data, MOST_FILE, &sd, &sl);
            if (sr == P_MISSING) {
                text_free(&src);
                continue;
            }
            bool ok_seen = sr == P_OK && sl == len(ARCHIVE_SEEN) && same(sd, ARCHIVE_SEEN);
            p_free(sd);
            if (!ok_seen) {
                text_free(&src), text_free(&post_p);
                add_report(report, it->id, "a seen mark is damaged");
                return S_DAMAGED;
            }
            arch_path(folder, it->month, &dst, it->id, it->seen.items[i]);
            s_result cr = dst.failed ? S_DAMAGED : copy_durable(src.data, dst.data, &why);
            text_free(&src), text_free(&dst);
            if (cr == S_DAMAGED) {
                text_free(&post_p);
                add_report(report, it->id, why ? why : "a seen mark cannot be preserved");
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
        bool ok_seen = sr == P_OK && sl == len(ARCHIVE_SEEN) && same(sd, ARCHIVE_SEEN);
        p_free(sd);
        if (!ok_seen) {
            message_free(&m);
            text_free(&post_p);
            add_report(report, it->id, "a seen mark is damaged");
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
    text dst_post = {0};
    arch_path(folder, it->month, &dst_post, it->id, "post");
    if (pause) pause(pause_user, "before-copy-post");
    s_result cr = dst_post.failed ? (why = "out of memory", S_DAMAGED) : copy_durable(post_p.data, dst_post.data, &why);
    text_free(&dst_post);
    if (cr == S_DAMAGED) {
        message_free(&m);
        text_free(&post_p);
        add_report(report, it->id, why);
        return S_DAMAGED;
    }
    if (pause) pause(pause_user, "after-copy-post");

    for (unsigned k = 0; k < j.links; k++) {
        text lnm = {0}, lsrc = {0}, ldst = {0};
        link_suffix(&lnm, k);
        if (!lnm.failed) {
            live_path(folder, &lsrc, it->id, lnm.data);
            arch_path(folder, it->month, &ldst, it->id, lnm.data);
        }
        if (pause) pause(pause_user, "before-copy-link");
        s_result lr = lnm.failed || lsrc.failed || ldst.failed ? (why = "out of memory", S_DAMAGED) : copy_durable(lsrc.data, ldst.data, &why);
        text_free(&lnm), text_free(&lsrc), text_free(&ldst);
        if (lr == S_DAMAGED) {
            message_free(&m);
            text_free(&post_p);
            add_report(report, it->id, why);
            return S_DAMAGED;
        }
    }
    if (pause) pause(pause_user, "after-copy-links");

    for (size_t i = 0; i < it->seen.count; i++) {
        text ssrc = {0}, sdst = {0};
        live_path(folder, &ssrc, it->id, it->seen.items[i]);
        arch_path(folder, it->month, &sdst, it->id, it->seen.items[i]);
        if (pause) pause(pause_user, "before-copy-seen");
        s_result sr = ssrc.failed || sdst.failed ? (why = "out of memory", S_DAMAGED) : copy_durable(ssrc.data, sdst.data, &why);
        text_free(&ssrc), text_free(&sdst);
        if (sr == S_DAMAGED) {
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
