#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "plat.h"

static p_result why(void) {
    return errno == ENOENT ? P_MISSING : errno == EEXIST ? P_EXISTS : P_FAILED;
}

void *p_alloc(size_t bytes) { return calloc(1, bytes ? bytes : 1); }
void p_free(void *memory) { free(memory); }

static p_result all(int file, size_t most, char **data, size_t *length) {
    char *buffer = malloc(most + 2);
    size_t have = 0;
    if (!buffer) return P_FAILED;
    for (;;) {
        ssize_t got = read(file, buffer + have, most + 1 - have);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0 || (have += (size_t)got) > most) {
            free(buffer);
            return got < 0 ? P_FAILED : P_TOO_LARGE;
        }
        if (!got) break;
    }
    buffer[have] = 0;
    *data = buffer, *length = have;
    return P_OK;
}

p_result p_read(const char *path, size_t most, char **data, size_t *length) {
    struct stat info;
    int file = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK); /* never a pipe, a link or a device */
    if (file < 0) return why();
    p_result result = fstat(file, &info) || !S_ISREG(info.st_mode) ? P_FAILED : all(file, most, data, length);
    close(file);
    return result;
}

p_result p_input(size_t most, char **data, size_t *length) { return all(0, most, data, length); }

/* The name is on the disk too, not only the bytes. */
#ifdef HELIO_TEST_HOOKS
bool plat_fail_flush = false;
bool plat_fail_sync = false;
uint64_t plat_now_override = 0;
#endif
static bool settle(const char *path) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_flush) return false;
#endif
    char folder[4096];
    const char *slash = strrchr(path, '/');
    if (slash == path) {
        folder[0] = '/', folder[1] = 0;
    } else {
        size_t n = slash ? (size_t)(slash - path) : 0;
        if (n >= sizeof folder) return false;
        memcpy(folder, n ? path : ".", n ? n : 1);
        folder[n ? n : 1] = 0;
    }
    int file = open(folder, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (file < 0) return false;
    bool good = !fsync(file);
    close(file);
    return good;
}

p_result p_write(const char *path, const char *data, size_t length, bool replace, const char *unique) {
    char temp[4096];
    if ((size_t)snprintf(temp, sizeof temp, "%s.%s.part", path, unique) >= sizeof temp) return P_FAILED;
    int file = open(temp, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (file < 0) return errno == ENOENT ? P_MISSING : P_FAILED; /* a taken temporary name says nothing about path */
    size_t done = 0;
    bool good = true;
    while (good && done < length) {
        ssize_t wrote = write(file, data + done, length - done);
        if (wrote < 0 && errno == EINTR) continue;
        good = wrote > 0;
        if (good) done += (size_t)wrote;
    }
    good = good && !fsync(file);
    good = !close(file) && good;
    p_result result = P_FAILED;
    if (good && replace) result = rename(temp, path) ? why() : P_OK;
    else if (good) result = link(temp, path) ? why() : P_OK; /* fails when the name is taken, on D: too */
    if (!(good && replace && result == P_OK)) unlink(temp);
    if (result == P_OK && !settle(path)) result = P_UNFLUSHED;
    return result;
}

p_result p_remove(const char *path) {
    if (unlink(path)) return why();
    return settle(path) ? P_OK : P_FAILED;
}

p_result p_sync(const char *path) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_flush || plat_fail_sync) return P_FAILED;
#endif
    int file = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (file < 0) return why();
    struct stat info;
    bool good = !fstat(file, &info) && S_ISREG(info.st_mode) && !fsync(file);
    close(file);
    return good && settle(path) ? P_OK : P_FAILED;
}

static bool sync_dir(const char *path) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_flush) return false;
#endif
    int file = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (file < 0) return false;
    bool good = !fsync(file);
    close(file);
    return good;
}

p_result p_folder(const char *path) {
    struct stat info;
    if (!mkdir(path, 0700)) return settle(path) && sync_dir(path) ? P_OK : P_FAILED;
    if (errno == EEXIST && !lstat(path, &info) && S_ISDIR(info.st_mode)) return settle(path) && sync_dir(path) ? P_OK : P_FAILED;
    return errno == EEXIST ? P_FAILED : why();
}

p_result p_list(const char *path, bool (*each)(void *user, const char *name), void *user) {
    DIR *folder = opendir(path);
    if (!folder) return why();
    errno = 0;
    for (struct dirent *entry; (entry = readdir(folder)); errno = 0)
        if (strcmp(entry->d_name, ".") && strcmp(entry->d_name, "..") && !each(user, entry->d_name)) break;
    p_result result = errno ? P_FAILED : P_OK;
    closedir(folder);
    return result;
}

uint64_t p_now(void) {
#ifdef HELIO_TEST_HOOKS
    if (plat_now_override) return plat_now_override;
#endif
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    return (uint64_t)t.tv_sec * 1000000 + (uint64_t)t.tv_nsec / 1000;
}

uint64_t p_tick(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return ((uint64_t)t.tv_nsec ^ (uint64_t)getpid() << 32) * 0x9E3779B97F4A7C15ull;
}

char *p_env(const char *name) {
    const char *value = getenv(name);
    return value ? strdup(value) : NULL;
}

bool p_print(const char *text) { return fputs(text, stdout) >= 0 && !fflush(stdout); }
bool p_error(const char *text) { return fputs(text, stderr) >= 0; }
