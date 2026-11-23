/* racer <store> <lane> <ids...>: waits for the file "go" in the store, then
   tries to take every job at full speed. The exit code is the number won. */
#include "store.h"

int racer(int count, char **args) {
    char *data;
    size_t length;
    text go = {0}, ready = {0};
    add(&ready, args[1]), add(&ready, "/ready."), add_number(&ready, p_tick(), 0);
    add(&go, args[1]), add(&go, "/go");
    if (p_write(ready.data, "x", 1, false, "r") != P_OK) return 100;
    for (uint64_t until = p_now() + 30000000; p_read(go.data, 9, &data, &length) != P_OK;)
        if (p_now() > until) return 101;
    store s = {.folder = args[1]};
    int won = 0;
    for (int i = 3; i < count; i++) {
        s_result result = store_take(&s, args[i], (actor){args[2], ""});
        if (result == S_OK) won++;
        else if (result != S_REFUSED) return 102;
    }
    return won;
}
