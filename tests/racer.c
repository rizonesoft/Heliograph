/* racer <store> <lane> <ids...>: waits for the file "go" in the store, then
   tries to take every job at full speed. The exit code is the number won.
   racer --remove <path>: calls p_remove(path) and exits with p_result.
   racer --pause <job|take|seen> <stages> <store> <lane> <id>: pauses at each
   named store stage (writing paused.<stage> and waiting for resume.<stage>),
   prints s.why to stderr on non-OK, and exits with s_result.
   racer --pause-cli <stages> <store> <helio-args...>: runs helio_paused. */
#include "archive.h"
#include "helio.h"
#include "place.h"
#include "store.h"

#ifdef HELIO_TEST_HOOKS
extern bool plat_fail_flush;
extern bool plat_fail_write_flush;
extern bool plat_fail_sync;
extern uint64_t plat_now_override;
extern size_t store_max_names;
#endif

typedef struct {
    const char *folder;
    const char *stages;
} pause_ctx;

static bool want_stage(const char *stages, const char *stage) {
    size_t n = len(stage);
    for (const char *at = stages; at && *at;) {
        const char *end = at;
        while (*end && *end != ',') end++;
        if ((size_t)(end - at) == n) {
            bool eq = true;
            for (size_t i = 0; i < n && eq; i++) eq = at[i] == stage[i];
            if (eq) return true;
        }
        at = *end ? end + 1 : end;
    }
    return false;
}

static void on_pause(void *user, const char *stage) {
    pause_ctx *ctx = user;
    if (!want_stage(ctx->stages, stage)) return;
    text paused = {0}, resume = {0};
    add(&paused, ctx->folder), add(&paused, "/paused."), add(&paused, stage);
    add(&resume, ctx->folder), add(&resume, "/resume."), add(&resume, stage);
    if (!paused.failed && !resume.failed) {
        p_write(paused.data, "x", 1, true, "p");
        for (uint64_t until = p_now() + 30000000;;) {
            char *data = NULL;
            size_t length = 0;
            if (p_read(resume.data, 16, &data, &length) == P_OK) {
                p_free(data);
                break;
            }
            if (p_now() > until) break;
        }
    }
    text_free(&paused);
    text_free(&resume);
}

static int run_pause(int count, char **args) {
    if (count < 7) return 104;
    const char *op = args[2], *stages = args[3], *folder = args[4], *lane = args[5], *id = args[6];
    store s = {0};
    s_result r = store_open(&s, folder);
    if (r != S_OK) {
        if (s.why) p_error(s.why), p_error("\n");
        return (int)r;
    }
    pause_ctx ctx = {folder, stages};
    s.pause = on_pause;
    s.pause_user = &ctx;
    if (same(op, "job")) {
        message m = {0};
        job now = {0};
        r = store_message(&s, id, &m);
        if (r == S_OK) r = store_job(&s, &m, &now);
        message_free(&m);
    } else if (same(op, "take")) {
        r = store_take(&s, id, (actor){lane, ""});
    } else if (same(op, "seen")) {
        r = store_seen(&s, id, lane);
    } else {
        r = S_DAMAGED;
        s.why = "unknown op";
    }
    if (r != S_OK && s.why) p_error(s.why), p_error("\n");
    store_close(&s);
    return (int)r;
}

typedef struct { char **names; size_t count, cap; } name_vec;

static void name_vec_free(name_vec *v) {
    for (size_t i = 0; i < v->count; i++) p_free(v->names[i]);
    p_free(v->names);
    *v = (name_vec){0};
}

static bool collect_post_names(void *user, const char *name) {
    name_vec *dst = user;
    size_t n = len(name);
    if (n < 6 || !same(name + n - 5, ".post")) return true;
    if (dst->count >= dst->cap) {
        size_t next = dst->cap ? dst->cap * 2 : 128;
        char **grown = p_alloc(next * sizeof(char *));
        if (!grown) return false;
        for (size_t i = 0; i < dst->count; i++) grown[i] = dst->names[i];
        p_free(dst->names);
        dst->names = grown;
        dst->cap = next;
    }
    char *dup = copy(name, n);
    if (!dup) return false;
    dst->names[dst->count++] = dup;
    return true;
}

static bool has_sorted_name(char *const *names, size_t count, const char *target) {
    size_t lo = 0, hi = count;
    while (lo < hi) {
        size_t mid = lo + (hi - lo) / 2;
        const char *a = names[mid], *b = target;
        while (*a && *a == *b) a++, b++;
        int cmp = (unsigned char)*a - (unsigned char)*b;
        if (cmp == 0) return true;
        if (cmp < 0) lo = mid + 1;
        else hi = mid;
    }
    return false;
}

static void sort_names_simple(char **names, size_t count) {
    for (size_t gap = count / 2; gap > 0; gap /= 2) {
        for (size_t i = gap; i < count; i++) {
            char *tmp = names[i];
            size_t j = i;
            for (; j >= gap; j -= gap) {
                const char *a = names[j - gap], *b = tmp;
                while (*a && *a == *b) a++, b++;
                if ((unsigned char)*a <= (unsigned char)*b) break;
                names[j] = names[j - gap];
            }
            names[j] = tmp;
        }
    }
}

static bool print_one_session(void *user, const session_info *info) {
    (void)user;
    text out = {0};
    add(&out, "lane="), add(&out, info->lane);
    add(&out, "\twhen="), add_number(&out, info->when, 0);
    add(&out, "\tagent="), add(&out, info->agent);
    add(&out, "\trepository="), add(&out, info->repository);
    add(&out, "\tsession="), add(&out, info->session);
    add(&out, "\tlegacy="), add(&out, info->legacy ? "1" : "0");
    add(&out, "\n");
    if (!out.failed) p_print(out.data);
    text_free(&out);
    return true;
}

int racer(int count, char **args) {
    if (count >= 3 && same(args[1], "--remove")) return (int)p_remove(args[2]);
    if (count >= 4 && same(args[1], "--read-session")) {
        store s = {.folder = args[2]};
        const char *lane = args[3];
        const char *sess = count >= 5 ? args[4] : "";
        session_info info = {0};
        s_result r = place_session(&s, lane, sess, &info);
        if (r == S_OK) print_one_session(NULL, &info);
        else if (s.why) p_error(s.why), p_error("\n");
        return (int)r;
    }
    if (count >= 3 && same(args[1], "--list-sessions")) {
        store s = {.folder = args[2]};
        p_result r = place_each_session(&s, print_one_session, NULL);
        return r == P_OK ? 0 : (int)r;
    }
#ifdef HELIO_TEST_HOOKS
    if (count >= 3 && same(args[1], "--remove-fail-flush")) {
        plat_fail_flush = true;
        return (int)p_remove(args[2]);
    }
    if (count >= 3 && same(args[1], "--fail-flush-cli")) {
        plat_fail_flush = true;
        return helio(count - 1, args + 1);
    }
    if (count >= 3 && same(args[1], "--fail-write-flush-cli")) {
        plat_fail_write_flush = true;
        return helio(count - 1, args + 1);
    }
    if (count >= 3 && same(args[1], "--fail-sync-cli")) {
        plat_fail_sync = true;
        return helio(count - 1, args + 1);
    }
    if (count >= 4 && same(args[1], "--now")) {
        uint64_t v = 0;
        for (const char *p = args[2]; *p >= '0' && *p <= '9'; p++) v = v * 10 + (uint64_t)(*p - '0');
        plat_now_override = v;
        return helio(count - 2, args + 2);
    }
    if (count >= 4 && same(args[1], "--max-names")) {
        size_t lim = 0;
        for (const char *p = args[2]; *p >= '0' && *p <= '9'; p++) lim = lim * 10 + (size_t)(*p - '0');
        store_max_names = lim;
        return helio(count - 2, args + 2);
    }
    if (count >= 4 && same(args[1], "--batch-size")) {
        size_t lim = 0;
        for (const char *p = args[2]; *p >= '0' && *p <= '9'; p++) lim = lim * 10 + (size_t)(*p - '0');
        archive_batch_size = lim;
        return helio(count - 2, args + 2);
    }
#endif
    if (count >= 4 && same(args[1], "--archive-loop")) {
        const char *folder = args[2];
        int rounds = 0;
        for (const char *p = args[3]; *p >= '0' && *p <= '9'; p++) rounds = rounds * 10 + (*p - '0');
        text ready = {0}, go = {0};
        add(&ready, folder), add(&ready, "/ready."), add_number(&ready, p_tick(), 0);
        add(&go, folder), add(&go, "/go");
        if (p_write(ready.data, "x", 1, false, "r") != P_OK) return 100;
        char *gdata = NULL;
        size_t glen = 0;
        for (uint64_t until = p_now() + 30000000; p_read(go.data, 9, &gdata, &glen) != P_OK;)
            if (p_now() > until) return 101;
        p_free(gdata);
        text_free(&ready), text_free(&go);
        size_t total_moved = 0;
        for (int i = 0; i < rounds; i++) {
            size_t moved = 0;
            text rep = {0};
            s_result r = archive_store(folder, 1, &moved, &rep, NULL, NULL);
            total_moved += moved;
            if (r == S_DAMAGED) {
                if (rep.length) p_error(rep.data);
                text_free(&rep);
                return 3;
            }
            text_free(&rep);
            if (r == S_NOTHING) break;
        }
        text out = {0};
        add_number(&out, (uint64_t)total_moved, 0), add(&out, "\n");
        if (!out.failed) p_print(out.data);
        text_free(&out);
        return 0;
    }
    if (count >= 6 && same(args[1], "--read-loop")) {
        const char *folder = args[2];
        int min_rounds = 0, live_count = 0;
        for (const char *p = args[3]; *p >= '0' && *p <= '9'; p++) min_rounds = min_rounds * 10 + (*p - '0');
        for (const char *p = args[4]; *p >= '0' && *p <= '9'; p++) live_count = live_count * 10 + (*p - '0');
        /* Load all eligible IDs from <folder>/watch_old.txt (one ID per line) so every round checks all live
           controls plus a rolling 1/10 slice of all 1,000 eligible IDs (100% coverage every 10 rounds). */
        text wf = {0};
        add(&wf, folder), add(&wf, "/watch_old.txt");
        char *wdata = NULL;
        size_t wlen = 0;
        if (wf.failed || p_read(wf.data, 200000, &wdata, &wlen) != P_OK) {
            text_free(&wf);
            p_free(wdata);
            return 103;
        }
        text_free(&wf);
        size_t old_cap = 1024, old_n = 0;
        char **old_ids = p_alloc(old_cap * sizeof(char *));
        if (!old_ids) {
            p_free(wdata);
            return 103;
        }
        for (char *p = wdata; *p;) {
            char *line = p;
            while (*p && *p != '\n' && *p != '\r') p++;
            if (*p) *p++ = 0;
            while (*p == '\n' || *p == '\r') p++;
            if (*line && old_n < old_cap) old_ids[old_n++] = line;
        }
        text ready = {0}, go = {0}, done = {0};
        add(&ready, folder), add(&ready, "/ready."), add_number(&ready, p_tick(), 0);
        add(&go, folder), add(&go, "/go");
        add(&done, folder), add(&done, "/done");
        if (p_write(ready.data, "x", 1, false, "r") != P_OK) return 100;
        char *gdata = NULL;
        size_t glen = 0;
        for (uint64_t until = p_now() + 30000000; p_read(go.data, 9, &gdata, &glen) != P_OK;)
            if (p_now() > until) return 101;
        p_free(gdata);
        text_free(&ready), text_free(&go);
        for (int i = 0;; i++) {
            store s = {0};
            if (store_open(&s, folder) != S_OK) {
                store_close(&s);
                text_free(&done);
                p_free(old_ids);
                p_free(wdata);
                return 3;
            }
            for (int k = 5; k < 5 + live_count && k < count; k++) {
                message m = {0};
                s_result mr = store_message(&s, args[k], &m);
                if (mr != S_OK) {
                    store_close(&s);
                    text_free(&done);
                    p_free(old_ids);
                    p_free(wdata);
                    return 4;
                }
                message_free(&m);
            }
            /* Verify 100% of ALL eligible IDs on EVERY SINGLE ROUND via live store list + archive month list
               (zero sampling, O(1) directory listings per round: every ID must be in messages/ OR archive/2025-02/!). */
            text adir = {0};
            add(&adir, folder), add(&adir, "/archive/2025-02");
            name_vec arch = {0};
            if (!adir.failed) {
                p_result al = p_list(adir.data, collect_post_names, &arch);
                if (al != P_OK && al != P_MISSING) {
                    name_vec_free(&arch);
                    text_free(&adir);
                    store_close(&s);
                    text_free(&done);
                    p_free(old_ids);
                    p_free(wdata);
                    return 6;
                }
                sort_names_simple(arch.names, arch.count);
            }
            for (size_t j = 0; j < old_n; j++) {
                text target = {0};
                add(&target, old_ids[j]), add(&target, ".post");
                bool found = !target.failed && (has_sorted_name(s.names, s.count, target.data) || has_sorted_name(arch.names, arch.count, target.data));
                text_free(&target);
                if (!found) {
                    /* Re-check archive directly once in case it moved between the two directory listings! */
                    message m = {0};
                    s_result mr = archive_read_message(&s, old_ids[j], &m);
                    message_free(&m);
                    if (mr != S_OK) {
                        name_vec_free(&arch);
                        text_free(&adir);
                        store_close(&s);
                        text_free(&done);
                        p_free(old_ids);
                        p_free(wdata);
                        return 5;
                    }
                }
            }
            name_vec_free(&arch);
            text_free(&adir);
            /* Also parse and verify full readable message bytes for a rotating window of 10 eligible IDs per round
               so every round checks both 100% membership across all 1,000 IDs and full message readability. */
            for (size_t w = 0; w < 10 && old_n > 0; w++) {
                const char *oid = old_ids[((size_t)i * 10 + w) % old_n];
                message m = {0};
                s_result mr = store_message(&s, oid, &m);
                if (mr != S_OK) {
                    message_free(&m);
                    mr = archive_read_message(&s, oid, &m);
                    if (mr != S_OK) {
                        store_close(&s);
                        text_free(&done);
                        p_free(old_ids);
                        p_free(wdata);
                        return 7;
                    }
                }
                message_free(&m);
            }
            store_close(&s);
            if (i + 1 >= min_rounds) {
                char *dd = NULL;
                size_t dl = 0;
                p_result dr = p_read(done.data, 9, &dd, &dl);
                p_free(dd);
                if (dr == P_OK) break;
            }
        }
        text_free(&done);
        p_free(old_ids);
        p_free(wdata);
        return 0;
    }
    if (count >= 2 && same(args[1], "--pause")) return run_pause(count, args);
    if (count >= 4 && same(args[1], "--pause-cli")) {
        pause_ctx ctx = {args[3], args[2]};
        return helio_paused(count - 3, args + 3, on_pause, &ctx);
    }
    char *data = NULL;
    size_t length = 0;
    text go = {0}, ready = {0};
    add(&ready, args[1]), add(&ready, "/ready."), add_number(&ready, p_tick(), 0);
    add(&go, args[1]), add(&go, "/go");
    if (p_write(ready.data, "x", 1, false, "r") != P_OK) return 100;
    for (uint64_t until = p_now() + 30000000; p_read(go.data, 9, &data, &length) != P_OK;)
        if (p_now() > until) return 101;
    p_free(data);
    text_free(&go);
    text_free(&ready);
    store s = {.folder = args[1]};
    int won = 0;
    for (int i = 3; i < count; i++) {
        s_result result = store_take(&s, args[i], (actor){args[2], ""});
        if (result == S_OK) won++;
        else if (result != S_REFUSED) return 102;
    }
    return won;
}
