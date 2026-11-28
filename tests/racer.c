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
#include "watch.h"
#include "json.h"

#ifdef HELIO_TEST_HOOKS
extern const char *helio_cwd_override;
extern bool plat_fail_flush;
extern bool plat_fail_write_flush;
extern bool plat_fail_sync;
extern uint64_t plat_now_override;
extern uint64_t plat_now_step;
extern void (*plat_pause)(const char *stage);
extern unsigned long plat_last_hold_error;
extern size_t store_max_names;
#endif

typedef struct {
    const char *folder;
    const char *stages;
    bool timed_out; /* a pause released by its 30-second limit, not by the test: the run proves nothing */
} pause_ctx;

enum { PAUSE_TIMED_OUT = 105 };

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
            if (p_now() > until) {
                ctx->timed_out = true;
                break;
            }
        }
    }
    text_free(&paused);
    text_free(&resume);
}

#ifdef HELIO_TEST_HOOKS
static pause_ctx *plat_ctx;
static void plat_on_pause(const char *stage) { on_pause(plat_ctx, stage); }

/* --dir <folder|sync|read> <root> <rel> <name> <stages>: one held-folder call on <name> inside <root>/<rel>,
   pausing at <stages> inside the platform (paused.<stage> and resume.<stage> in <root>); exits with its p_result. */
static int run_dir(int count, char **args) {
    if (count < 7) return 104;
    const char *op = args[2], *root = args[3], *rel = args[4], *name = args[5];
    pause_ctx ctx = {root, count > 6 ? args[6] : "", false};
    plat_ctx = &ctx, plat_pause = plat_on_pause;
    p_dir *d = NULL;
    p_result r = p_dir_open(root, rel, &d);
    if (r == P_OK) {
        char *data = NULL;
        size_t length = 0;
        if (same(op, "folder")) r = p_dir_folder(d, name);
        else if (same(op, "sync")) r = p_dir_sync(d, name);
        else if (same(op, "read")) r = p_dir_read(d, name, 1 << 20, &data, &length), p_free(data);
        else r = P_FAILED;
    }
    p_dir_close(d);
    plat_pause = NULL;
    return ctx.timed_out ? PAUSE_TIMED_OUT : (int)r;
}

/* --pause-lifetime <store> <id> <reader>: shows <id> as <reader> through helio_paused with a counting pause, then
   reads it from the archive directly; exits 0 when the pause was never called after helio_paused returned. */
static int lifetime_calls;
static void count_pause(void *user, const char *stage) { (void)user, (void)stage, lifetime_calls++; }
static int run_pause_lifetime(int count, char **args) {
    if (count < 5) return 104;
    char *show[] = {"helio", "show", args[3], "--store", args[2], "--as", args[4]};
    int code = helio_paused(7, show, count_pause, NULL);
    if (code != 0) return 100 + code;
    int during = lifetime_calls;
    store s = {.folder = args[2]};
    message m = {0};
    s_result r = archive_read_message(&s, args[3], &m);
    message_free(&m);
    if (r != S_OK) return 90;
    return during > 0 && lifetime_calls == during ? 0 : 91;
}
#endif

static int run_pause(int count, char **args) {
    if (count < 7) return 104;
    const char *op = args[2], *stages = args[3], *folder = args[4], *lane = args[5], *id = args[6];
    store s = {0};
    s_result r = store_open(&s, folder);
    if (r != S_OK) {
        if (s.why) p_error(s.why), p_error("\n");
        return (int)r;
    }
    pause_ctx ctx = {folder, stages, false};
    s.pause = on_pause;
    s.pause_user = &ctx;
    if (same(op, "job")) {
        message m = {0};
        job now = {0};
        r = store_message(&s, id, &m);
        if (r == S_OK) r = store_job(&s, &m, &now);
        message_free(&m);
    } else if (same(op, "take")) {
        r = store_take(&s, id, (actor){.lane = lane, .session = ""}, NULL);
    } else if (same(op, "seen")) {
        r = store_seen(&s, id, (actor){.lane = lane, .session = ""});
    } else {
        r = S_DAMAGED;
        s.why = "unknown op";
    }
    if (r != S_OK && s.why) p_error(s.why), p_error("\n");
    store_close(&s);
    if (ctx.timed_out) return p_error("racer: a pause timed out before the test resumed it\n"), PAUSE_TIMED_OUT;
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
    add(&out, "\tnewest="), add_number(&out, info->newest > info->when ? info->newest : info->when, 0);
    add(&out, "\tagent="), add(&out, info->agent);
    add(&out, "\trepository="), add(&out, info->repository);
    add(&out, "\tsession="), add(&out, info->session);
    add(&out, "\tlegacy="), add(&out, info->legacy ? "1" : "0");
    add(&out, "\n");
    if (!out.failed) p_print(out.data);
    text_free(&out);
    return true;
}

static bool any_taken_step(void *user, const char *name) {
    size_t n = len(name);
    if (n > 10 && same(name + n - 10, ".link.0000")) *(bool *)user = true;
    return true;
}

int racer(int count, char **args) {
    if (count >= 3 && same(args[1], "--remove")) return (int)p_remove(args[2]);
    /* --terminal-loop <store> <id> <rounds> <folder>: asks whether <id> has an archived terminal copy <rounds>
       times; exit 9 the first time the answer is damage, 8 the first time it is anything but nothing (the
       fixture archives nothing), 0 when it is always nothing, printing how many times it found
       <folder> present and absent, so a test knows the folder really came and went while it looked. */
    if (count >= 6 && same(args[1], "--terminal-loop")) {
        unsigned long rounds = 0;
        const char *p = args[4];
        for (; *p >= '0' && *p <= '9'; p++) rounds = rounds * 10 + (unsigned long)(*p - '0');
        if (*p || !rounds) return 104;
        uint64_t present = 0, absent = 0;
        for (unsigned long i = 0; i < rounds; i++) {
            p_result seen = p_real_dir(args[5]);
            present += seen == P_OK, absent += seen == P_MISSING;
            s_result got = archive_has_terminal_job(args[2], args[3], NULL);
            if (got == S_DAMAGED) {
                text why = {0};
                add(&why, "racer: damage in round "), add_number(&why, i, 0), add(&why, ", last refused hold error "),
                    add_number(&why, plat_last_hold_error, 0), add(&why, "\n");
                if (!why.failed) p_error(why.data);
                text_free(&why);
                return 9;
            }
            if (got != S_NOTHING) return 8;
        }
        text out = {0};
        add_number(&out, present, 0), add(&out, " "), add_number(&out, absent, 0), add(&out, "\n");
        if (!out.failed) p_print(out.data);
        text_free(&out);
        return 0;
    }
    /* --json-escape-many: stdin holds records of a 4-byte big-endian length and that many bytes; prints the JSON string
       of each on its own line. */
    if (count >= 2 && same(args[1], "--json-escape-many")) {
        char *in = NULL;
        size_t n = 0;
        if (p_input(64u << 20, &in, &n) != P_OK) return 100;
        text out = {0};
        for (size_t at = 0; at + 4 <= n;) {
            const unsigned char *h = (const unsigned char *)in + at;
            size_t k = ((size_t)h[0] << 24) | ((size_t)h[1] << 16) | ((size_t)h[2] << 8) | h[3];
            at += 4;
            if (k > n - at) break;
            json_string(&out, in + at, k), add(&out, "\n");
            at += k;
        }
        p_free(in);
        bool good = !out.failed && (!out.data || p_print(out.data));
        text_free(&out);
        return good ? 0 : 3;
    }
    /* --cwd <working folder> <helio words>: init resolves its folder against that working folder. */
    if (count >= 4 && same(args[1], "--cwd")) {
        helio_cwd_override = args[2];
        return helio(count - 2, args + 2);
    }
    /* --absolute <working folder> <folder>: the folder as an absolute path from that working folder (exit 2, and the
       reason on standard error, when it cannot be resolved), so Windows rules are tested on any host. */
    if (count >= 4 && same(args[1], "--absolute")) {
        text out = {0};
        const char *why = NULL;
        s_result r = place_absolute(&out, args[2], args[3], &why);
        if (r != S_OK) {
            p_error(why ? why : "failed");
            p_error("\n");
            text_free(&out);
            return r == S_REFUSED ? 2 : 3;
        }
        add(&out, "\n");
        bool good = !out.failed && p_print(out.data);
        text_free(&out);
        return good ? 0 : 3;
    }
    /* --json-escape: the JSON string of the bytes on stdin, and a newline. */
    if (count >= 2 && same(args[1], "--json-escape")) {
        char *in = NULL;
        size_t n = 0;
        if (p_input(1 << 20, &in, &n) != P_OK) return 100;
        text out = {0};
        json_string(&out, in, n);
        add(&out, "\n");
        p_free(in);
        bool good = !out.failed && p_print(out.data);
        text_free(&out);
        return good ? 0 : 3;
    }
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
    if (count >= 7 && same(args[1], "--dir")) return run_dir(count, args);
    if (count >= 5 && same(args[1], "--pause-lifetime")) return run_pause_lifetime(count, args);
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
        /* --now <us> --pause-cli ... or --fail-*-cli ...: that racer option under the fixed clock. */
        if (same(args[3], "--pause-cli") || (starts(args[3], "--fail-") && len(args[3]) > 10 && same(args[3] + len(args[3]) - 4, "-cli")))
            return racer(count - 2, args + 2);
        return helio(count - 2, args + 2);
    }
    if (count >= 5 && same(args[1], "--now-step")) { /* --now-step <us> <step> <helio args>: a clock that moves on by step at each reading */
        uint64_t v = 0, step = 0;
        for (const char *p = args[2]; *p >= '0' && *p <= '9'; p++) v = v * 10 + (uint64_t)(*p - '0');
        for (const char *p = args[3]; *p >= '0' && *p <= '9'; p++) step = step * 10 + (uint64_t)(*p - '0');
        plat_now_override = v, plat_now_step = step;
        return helio(count - 3, args + 3);
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
    if (count >= 2 && same(args[1], "--test-is-silent")) {
        const uint64_t T = 1000000000000ull; /* 1e12 us */
        const uint64_t M = 60000000ull;      /* 1 min in us */
        session_info sess[4] = {
            {.when = T - 10 * M, .lane = "os/apps", .session = "s-alive"},
            {.when = T - 200 * M, .lane = "os/apps", .session = "s-quiet"},
            {.when = T - 30 * M, .lane = "os/ui", .session = "s-ui"},
            {.when = T - 150 * M, .lane = "os/net", .session = ""},
        };
        /* 1. Active named session -> not silent. */
        job j1 = {.state = TAKEN, .when = T - 300 * M, .by = "os/apps", .session = "s-alive"};
        if (is_silent(sess, 4, &j1, T, 120 * M)) return 10;
        /* 2. Quiet named session, EVEN THOUGH sibling s-alive on same lane is active -> SILENT! */
        job j2 = {.state = TAKEN, .when = T - 300 * M, .by = "os/apps", .session = "s-quiet"};
        if (!is_silent(sess, 4, &j2, T, 120 * M)) return 11;
        /* 3. Named session missing from sessions[] -> falls back to j.when: recent take is alive, old take is silent. */
        job j3a = {.state = TAKEN, .when = T - 30 * M, .by = "os/apps", .session = "s-missing"};
        if (is_silent(sess, 4, &j3a, T, 120 * M)) return 12;
        job j3b = {.state = TAKEN, .when = T - 130 * M, .by = "os/apps", .session = "s-missing"};
        if (!is_silent(sess, 4, &j3b, T, 120 * M)) return 13;
        /* 4. Unnamed taker (session=="") -> uses max across ALL sessions of that lane (s-alive keeps os/apps alive). */
        job j4a = {.state = TAKEN, .when = T - 300 * M, .by = "os/apps", .session = ""};
        if (is_silent(sess, 4, &j4a, T, 120 * M)) return 14;
        /* 5. Unnamed taker whose only lane session is quiet -> SILENT. */
        job j4b = {.state = TAKEN, .when = T - 300 * M, .by = "os/net", .session = ""};
        if (!is_silent(sess, 4, &j4b, T, 120 * M)) return 15;
        /* 6. Boundary: exact == silent_us is silent; silent_us - 1 is not silent. */
        job j5 = {.state = TAKEN, .when = T - 120 * M, .by = "os/none", .session = "x"};
        if (!is_silent(sess, 4, &j5, T, 120 * M)) return 16;
        if (is_silent(sess, 4, &j5, T - 1, 120 * M)) return 17;
        /* 7. Clock skew (clock_now <= last) -> never silent, no underflow. */
        if (is_silent(sess, 4, &j5, T - 200 * M, 120 * M)) return 18;
        /* 8. Non-TAKEN states (OPEN, DONE, REFUSED) -> never silent. */
        job j6 = {.state = OPEN, .when = T - 500 * M, .by = "os/net", .session = ""};
        if (is_silent(sess, 4, &j6, T, 120 * M)) return 19;
        j6.state = DONE;
        if (is_silent(sess, 4, &j6, T, 120 * M)) return 20;
        j6.state = REFUSED;
        if (is_silent(sess, 4, &j6, T, 120 * M)) return 21;
        /* 9. Fresh take overrides stale session file (max(session.when, j.when)). */
        job j7 = {.state = TAKEN, .when = T - 5 * M, .by = "os/apps", .session = "s-quiet"};
        if (is_silent(sess, 4, &j7, T, 120 * M)) return 22;
        return 0;
    }
    /* --expiry <kind> <from> <to> <for-session or -> <sent-us> <now-us> [seen.<mark>...] [life:<lane>@<us>...]:
       prints archive_expiry's answer (not-yet, held or ready) for that message, with no store open. */
    if (count >= 8 && same(args[1], "--expiry")) {
        text id = {0};
        uint64_t sent = 0, now = 0;
        for (const char *p = args[6]; *p; p++) sent = sent * 10 + (uint64_t)(*p - '0');
        for (const char *p = args[7]; *p; p++) now = now * 10 + (uint64_t)(*p - '0');
        add_number(&id, sent, 0), add(&id, "-abc123");
        message m = {.id = id.data, .time = "", .kind = args[2], .from = args[3], .to = args[4], .for_session = same(args[5], "-") ? "" : args[5], .repository = ""};
        char *seen[64];
        expiry_life life[64];
        size_t ns = 0, nl = 0;
        if (count - 8 > 64) return p_error("racer: at most 64 marks and heartbeats\n"), 2;
        for (int i = 8; i < count; i++) {
            if (starts(args[i], "life:")) {
                const char *at = args[i] + 5;
                size_t n = 0;
                while (at[n] && at[n] != '@' && n + 1 < sizeof life[nl].lane) life[nl].lane[n] = at[n], n++;
                life[nl].lane[n] = 0;
                uint64_t w = 0;
                for (const char *p = at[n] == '@' ? at + n + 1 : ""; *p; p++) w = w * 10 + (uint64_t)(*p - '0');
                life[nl++].when = w;
            } else {
                seen[ns++] = args[i];
            }
        }
        expiry e = archive_expiry(&m, seen, ns, life, nl, now);
        text_free(&id);
        return p_print(e == EXPIRY_READY ? "ready\n" : e == EXPIRY_HELD ? "held\n" : "not-yet\n") ? 0 : 3;
    }
    if (count >= 2 && same(args[1], "--test-may-see")) {
        static const struct { const char *from, *to, *lane; bool expect; } cases[] = {
            {"os/main", "os/apps", "os/ui", true},
            {"os/main", "os/all", "os/ui", true},
            {"alpha/dev", "beta/dev", "alpha/other", true},
            {"alpha/dev", "beta/dev", "beta/other", true},
            {"alpha/dev", "beta/dev", "beta/dev", true},
            {"alpha/dev", "beta/dev", "gamma/dev", false},
            {"os/main", "os/apps", "os2/apps", false},
            {"os2/main", "os2/apps", "os/apps", false},
            {"os/main", "os/apps", "", false},
            {"os/main", "os/apps", "os/all", false},
            {"os/main", "os/apps", "badlane", false},
        };
        for (size_t i = 0; i < sizeof cases / sizeof *cases; i++) {
            message m = {.from = cases[i].from, .to = cases[i].to};
            if (may_see(&m, cases[i].lane) != cases[i].expect) return (int)(10 + i);
        }
        static const struct { const char *path; size_t root; bool win; } roots[] = {
            {"/", 1, false},
            {"/tmp/repo\\", 1, false},
            {"C:\\", 3, true},
            {"C:\\work\\sub", 3, true},
            {"\\\\server\\share\\work", 14, true},
            {"\\\\?\\C:\\work\\sub", 7, true},
            {"\\\\?\\UNC\\server\\share\\work", 20, true},
            {"\\\\?\\unc\\srv\\shr", 15, true},
            {"\\\\.\\C:\\work", 7, true},
            {"relative/path", 0, false},
        };
        for (size_t i = 0; i < sizeof roots / sizeof *roots; i++) {
            bool w = false;
            size_t got = place_path_root_len(roots[i].path, &w);
            if (got != roots[i].root || w != roots[i].win) return (int)(40 + i);
        }
        return 0;
    }
    /* --queue <store folder> <lane> [after-first]: a queue driver. It says it is ready, waits for <folder>/go, then runs the queue form of
       take (helio take --kind request --lane <lane>) until the queue is empty, one job id a line on standard output
       (the working folder holds the helio.conf); with after-first it waits until some job is taken first. Exits with the number of jobs it took, 101 when go never came, 102
       when a call failed. */
    if (count >= 4 && same(args[1], "--queue")) {
        char *data = NULL;
        size_t length = 0;
        text go = {0}, ready = {0};
        add(&ready, args[2]), add(&ready, "/ready."), add_number(&ready, p_tick(), 0);
        add(&go, args[2]), add(&go, "/go");
        if (p_write(ready.data, "x", 1, false, "r") != P_OK) return 100;
        for (uint64_t until = p_now() + 30000000; p_read(go.data, 9, &data, &length) != P_OK;)
            if (p_now() > until) return 101;
        p_free(data);
        text_free(&go);
        text_free(&ready);
        if (count >= 5 && same(args[4], "after-first")) { /* wait until some job has been taken, by anyone */
            text messages = {0};
            add(&messages, args[2]), add(&messages, "/messages");
            for (uint64_t until = p_now() + 30000000; !messages.failed && p_now() < until;) {
                bool taken = false;
                p_list(messages.data, any_taken_step, &taken);
                if (taken) break;
            }
            text_free(&messages);
        }
        char *call[] = {"helio", "take", "--kind", "request", "--lane", args[3], "--store", args[2]};
        int took = 0;
        for (;;) {
            int rc = helio((int)(sizeof call / sizeof *call), call);
            if (rc == 1) return took;
            if (rc != 0) return 102;
            took++;
        }
    }
    if (count >= 2 && same(args[1], "--pause")) return run_pause(count, args);
    if (count >= 4 && same(args[1], "--pause-cli")) {
        pause_ctx ctx = {args[3], args[2], false};
        int code = helio_paused(count - 3, args + 3, on_pause, &ctx);
        if (ctx.timed_out) return p_error("racer: a pause timed out before the test resumed it\n"), PAUSE_TIMED_OUT;
        return code;
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
        s_result result = store_take(&s, args[i], (actor){.lane = args[2], .session = ""}, NULL);
        if (result == S_OK) won++;
        else if (result != S_REFUSED) {
            p_error("racer: take "), p_error(args[i]), p_error(result == S_NOTHING ? ": nothing: " : ": damaged: ");
            p_error(s.why ? s.why : "(no reason)"), p_error("\n");
            return 102;
        }
    }
    return won;
}
