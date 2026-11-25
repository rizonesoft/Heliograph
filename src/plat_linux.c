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

static bool passing_linux(int attempt, int err) {
    if (attempt >= 8 || (err != EACCES && err != EBUSY && err != EPERM && err != ESTALE && err != EIO)) return false;
    struct timespec ts = {0, (long)(1000000u << attempt)};
    nanosleep(&ts, NULL);
    return true;
}

p_result p_read(const char *path, size_t most, char **data, size_t *length) {
    struct stat info;
    for (int attempt = 0;; attempt++) {
        int file = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK); /* never a pipe, a link or a device */
        if (file < 0) {
            if (passing_linux(attempt, errno)) continue;
            return why();
        }
        p_result result = fstat(file, &info) || !S_ISREG(info.st_mode) ? P_FAILED : all(file, most, data, length);
        int err = errno;
        close(file);
        if (result == P_FAILED && passing_linux(attempt, err)) continue;
        return result;
    }
}

p_result p_input(size_t most, char **data, size_t *length) { return all(0, most, data, length); }

/* The name is on the disk too, not only the bytes. */
#ifdef HELIO_TEST_HOOKS
bool plat_fail_flush = false;
bool plat_fail_write_flush = false;
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
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_sync) good = false;
#endif
    good = good && !fsync(file);
    good = !close(file) && good;
    p_result result = P_FAILED;
    if (good && replace) result = rename(temp, path) ? why() : P_OK;
    else if (good) result = link(temp, path) ? why() : P_OK; /* fails when the name is taken, on D: too */
    if (!(good && replace && result == P_OK)) unlink(temp);
#ifdef HELIO_TEST_HOOKS
    if (result == P_OK && plat_fail_write_flush) return P_UNFLUSHED;
#endif
    if (result == P_OK && !settle(path)) result = P_UNFLUSHED;
    return result;
}

p_result p_create_excl(const char *path, const char *data, size_t length) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_sync) return P_FAILED;
#endif
    if (length > 0) {
        static unsigned long long excl_seq = 0;
        char temp[4096], slot[4096];
        unsigned long long tick = (unsigned long long)p_tick();
        unsigned long pid = (unsigned long)getpid();
        unsigned long long seq = ++excl_seq;
        if ((size_t)snprintf(temp, sizeof temp, "%s.ex%lu-%llu-%llu.part", path, pid, tick, seq) >= sizeof temp) return P_FAILED;
        if ((size_t)snprintf(slot, sizeof slot, "%s.slot", path) >= sizeof slot) return P_FAILED;
        int tf = open(temp, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
        if (tf < 0) return errno == ENOENT ? P_MISSING : P_FAILED;
        size_t done = 0;
        bool good = true;
        while (good && done < length) {
            ssize_t wrote = write(tf, data + done, length - done);
            if (wrote < 0 && errno == EINTR) continue;
            good = wrote > 0;
            if (good) done += (size_t)wrote;
        }
        good = good && !fsync(tf);
        good = !close(tf) && good;
        if (!good) {
            unlink(temp);
            return P_FAILED;
        }
        /* Atomically claim the exclusive slot so cross-host Linux and Windows writers
           never race on the same path, then publish the complete flushed file via link. */
        int sf = open(slot, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
        if (sf < 0) {
            p_result sr = why();
            unlink(temp);
            return sr;
        }
        close(sf);
        p_result lr = link(temp, path) ? why() : P_OK;
        unlink(temp);
        unlink(slot);
        if (lr != P_OK) return lr;
#ifdef HELIO_TEST_HOOKS
        if (plat_fail_write_flush) return P_UNFLUSHED;
#endif
        return settle(path) ? P_OK : P_UNFLUSHED;
    }
    int file = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (file < 0) return why();
    bool good = !fsync(file);
    good = !close(file) && good;
    if (!good) {
        unlink(path);
        return P_FAILED;
    }
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_write_flush) return P_UNFLUSHED;
#endif
    return settle(path) ? P_OK : P_UNFLUSHED;
}

p_result p_file_mtime(const char *path, uint64_t *mtime_us) {
    struct stat st;
    if (lstat(path, &st) < 0) return why();
    if (st.st_mtim.tv_sec < 0) return P_FAILED;
    *mtime_us = (uint64_t)st.st_mtim.tv_sec * 1000000ull + (uint64_t)st.st_mtim.tv_nsec / 1000ull;
    return P_OK;
}

p_result p_claim_remove(const char *path, const char *unique) {
    char temp[4096];
    if ((size_t)snprintf(temp, sizeof temp, "%s.%s.rm.part", path, unique) >= sizeof temp) return P_FAILED;
    for (int attempt = 0;; attempt++) {
        if (!rename(path, temp)) break;
        if (!passing_linux(attempt, errno)) return why();
    }
    for (int attempt = 0;; attempt++) {
        if (!unlink(temp)) break;
        if (!passing_linux(attempt, errno)) return errno == ENOENT ? (settle(path) ? P_OK : P_FAILED) : P_FAILED;
    }
    return settle(path) ? P_OK : P_FAILED;
}

p_result p_remove(const char *path) {
    for (int attempt = 0;; attempt++) {
        if (!unlink(path)) break;
        if (!passing_linux(attempt, errno)) return why();
    }
    return settle(path) ? P_OK : P_FAILED;
}

p_result p_sync(const char *path) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_flush || plat_fail_sync) return P_FAILED;
#endif
    int file = -1;
    for (int attempt = 0;; attempt++) {
        file = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        if (file >= 0 || !passing_linux(attempt, errno)) break;
    }
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

bool p_is_real_dir(const char *path) {
    struct stat info;
    return !lstat(path, &info) && S_ISDIR(info.st_mode);
}

p_result p_list(const char *path, bool (*each)(void *user, const char *name), void *user) {
    int fd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return why();
    DIR *folder = fdopendir(fd);
    if (!folder) {
        int saved = errno;
        close(fd);
        errno = saved;
        return why();
    }
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
