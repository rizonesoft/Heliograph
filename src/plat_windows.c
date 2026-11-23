#include <impossible/file.h>
#include <impossible/text.h>
#include <windows.h>
#include "plat.h"

static p_result from(im_result result) {
    return (p_result)result;
}
_Static_assert((int)IM_FAILED == (int)P_FAILED && (int)IM_EXISTS == (int)P_EXISTS && (int)IM_TOO_LARGE == (int)P_TOO_LARGE, "the same order");

void *p_alloc(size_t bytes) { return im_alloc(bytes); }
void p_free(void *memory) { im_free(memory); }
p_result p_read(const char *path, size_t most, char **data, size_t *length) { return from(im_read(path, most, data, length)); }
p_result p_write(const char *path, const char *data, size_t length, bool replace, const char *unique) { return from(im_write(path, data, length, replace, unique)); }
p_result p_folder(const char *path) { return from(im_make_folder(path)); }
p_result p_input(size_t most, char **data, size_t *length) { return from(im_read_input(most, data, length)); }
uint64_t p_now(void) { return im_now(); }
bool p_print(const char *text) { return im_print(text); }

uint64_t p_tick(void) {
    LARGE_INTEGER count;
    QueryPerformanceCounter(&count);
    void *here = im_alloc(16); /* the heap's address differs between programs */
    uint64_t mixed = (uint64_t)count.QuadPart * 0x9E3779B97F4A7C15ull ^ (uintptr_t)here >> 4;
    im_free(here);
    return mixed;
}

typedef struct { bool (*each)(void *, const char *); void *user; } listing;
static bool entry(void *user, const char *name, bool folder) {
    listing *l = user;
    (void)folder; /* reported like a file, as on Linux: what cannot be read is damage */
    return l->each(l->user, name);
}
p_result p_list(const char *path, bool (*each)(void *user, const char *name), void *user) {
    listing l = {each, user};
    return from(im_list(path, entry, &l));
}

char *p_env(const char *name) {
    wchar_t *wide = im_wide(name);
    if (!wide) return NULL;
    char *value = NULL;
    DWORD need = GetEnvironmentVariableW(wide, NULL, 0);
    wchar_t *text = need ? im_alloc((size_t)need * sizeof *text) : NULL;
    if (text && GetEnvironmentVariableW(wide, text, need) < need) value = im_utf8(text);
    im_free(text);
    im_free(wide);
    return value;
}

bool p_error(const char *text) {
    HANDLE out = GetStdHandle(STD_ERROR_HANDLE);
    DWORD done;
    return out && out != INVALID_HANDLE_VALUE && WriteFile(out, text, (DWORD)im_length(text), &done, NULL);
}
