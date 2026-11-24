/* What Heliograph needs from a system. plat_windows.c and plat_linux.c give it. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum { P_OK, P_EXISTS, P_MISSING, P_TOO_LARGE, P_FAILED, P_UNFLUSHED } p_result;

void *p_alloc(size_t bytes); /* zeroed */
void p_free(void *memory);
p_result p_read(const char *path, size_t most, char **data, size_t *length);
/* Complete or not at all. Without replace the second writer gets P_EXISTS. */
p_result p_write(const char *path, const char *data, size_t length, bool replace, const char *unique);
p_result p_remove(const char *path);
p_result p_folder(const char *path);
p_result p_list(const char *path, bool (*each)(void *user, const char *name), void *user);
p_result p_input(size_t most, char **data, size_t *length);
uint64_t p_now(void); /* microseconds since 1970 */
uint64_t p_tick(void); /* differs between programs started together */
char *p_env(const char *name); /* new memory or NULL */
bool p_print(const char *text);
bool p_error(const char *text);
