/* racer <store> <lane> <ids...>: waits for the file "go" in the store, then
   tries to take every job at full speed. The exit code is the number won.
   racer --remove <path>: calls p_remove(path) and exits with p_result.
   racer --pause <job|take|seen> <stages> <store> <lane> <id>: pauses at each
   named store stage (writing paused.<stage> and waiting for resume.<stage>),
   prints s.why to stderr on non-OK, and exits with s_result.
   racer --pause-cli <stages> <store> <helio-args...>: runs helio_paused. */
#include "archive.h"
#include "helio.h"
#include "store.h"

#ifdef HELIO_TEST_HOOKS
extern bool plat_fail_flush;
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

int racer(int count, char **args) {
    if (count >= 3 && same(args[1], "--remove")) return (int)p_remove(args[2]);
#ifdef HELIO_TEST_HOOKS
    if (count >= 3 && same(args[1], "--remove-fail-flush")) {
        plat_fail_flush = true;
        return (int)p_remove(args[2]);
    }
    if (count >= 3 && same(args[1], "--fail-flush-cli")) {
        plat_fail_flush = true;
        return helio(count - 1, args + 1);
    }
    if (count >= 4 && same(args[1], "--max-names")) {
        size_t lim = 0;
        for (const char *p = args[2]; *p >= '0' && *p <= '9'; p++) lim = lim * 10 + (size_t)(*p - '0');
        store_max_names = lim;
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
        for (int i = 0; i < rounds; i++) {
            size_t moved = 0;
            text rep = {0};
            s_result r = archive_store(folder, 0, &moved, &rep, NULL, NULL);
            if (r == S_DAMAGED) {
                if (rep.length) p_error(rep.data);
                text_free(&rep);
                return 3;
            }
            text_free(&rep);
        }
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
