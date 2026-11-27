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

static p_result read_at(int dir, const char *path, size_t most, char **data, size_t *length) {
    struct stat info;
    for (int attempt = 0;; attempt++) {
        int file = openat(dir, path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK); /* never a pipe, a link or a device */
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

p_result p_read(const char *path, size_t most, char **data, size_t *length) { return read_at(AT_FDCWD, path, most, data, length); }

p_result p_input(size_t most, char **data, size_t *length) { return all(0, most, data, length); }

/* The name is on the disk too, not only the bytes. */
#ifdef HELIO_TEST_HOOKS
bool plat_fail_flush = false;
bool plat_fail_write_flush = false;
bool plat_fail_sync = false;
uint64_t plat_now_override = 0;
void (*plat_pause)(const char *stage) = NULL; /* called between steps of a platform call, for a test to act */
unsigned long plat_last_hold_error = 0; /* the errno of the last refused folder hold, for a test to report */
#endif
/* Flushes the folder that holds path: the held folder itself when dir is one, else path's parent. */
static bool settle_at(int dir, const char *path) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_flush) return false;
#endif
    if (dir != AT_FDCWD) return !fsync(dir);
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

static p_result write_at(int dir, const char *path, const char *data, size_t length, bool replace, const char *unique) {
    char temp[4096];
    if ((size_t)snprintf(temp, sizeof temp, "%s.%s.part", path, unique) >= sizeof temp) return P_FAILED;
    int file = openat(dir, temp, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
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
    if (good && replace) result = renameat(dir, temp, dir, path) ? why() : P_OK;
    else if (good) result = linkat(dir, temp, dir, path, 0) ? why() : P_OK; /* fails when the name is taken, on D: too */
    if (!(good && replace && result == P_OK)) unlinkat(dir, temp, 0);
#ifdef HELIO_TEST_HOOKS
    if (result == P_OK && plat_fail_write_flush) return P_UNFLUSHED;
#endif
    if (result == P_OK && !settle_at(dir, path)) result = P_UNFLUSHED;
    return result;
}

p_result p_write(const char *path, const char *data, size_t length, bool replace, const char *unique) {
    return write_at(AT_FDCWD, path, data, length, replace, unique);
}

p_result p_create_excl(const char *path, const char *data, size_t length) {
    const int dir = AT_FDCWD;
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_sync) return P_FAILED;
#endif
    if (length > 0) {
        static unsigned long long excl_seq = 0;
        char temp[4096];
        const char *slash = strrchr(path, '/');
        size_t dirlen = slash ? (size_t)(slash - path + 1) : 0;
        unsigned long long tick = (unsigned long long)p_tick();
        unsigned long pid = (unsigned long)getpid();
        unsigned long long seq = ++excl_seq;
        if ((size_t)snprintf(temp, sizeof temp, "%.*s.ex%lu-%llu-%llu.part", (int)dirlen, path, pid, tick, seq) >= sizeof temp) return P_FAILED;
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
        /* Atomically publish the complete flushed file without replacing an existing destination. */
        p_result lr = link(temp, path) ? why() : P_OK;
        unlink(temp);
        if (lr != P_OK) return lr;
#ifdef HELIO_TEST_HOOKS
        if (plat_fail_write_flush) return P_UNFLUSHED;
#endif
        return settle_at(dir, path) ? P_OK : P_UNFLUSHED;
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
    return settle_at(dir, path) ? P_OK : P_UNFLUSHED;
}

p_result p_file_mtime(const char *path, uint64_t *mtime_us) {
    struct stat st;
    if (lstat(path, &st) < 0) return why();
    if (st.st_mtim.tv_sec < 0) return P_FAILED;
    *mtime_us = (uint64_t)st.st_mtim.tv_sec * 1000000ull + (uint64_t)st.st_mtim.tv_nsec / 1000ull;
    return P_OK;
}

p_result p_claim_remove(const char *path, const char *unique) {
    const int dir = AT_FDCWD;
    char temp[4096];
    if ((size_t)snprintf(temp, sizeof temp, "%s.%s.rm.part", path, unique) >= sizeof temp) return P_FAILED;
    for (int attempt = 0;; attempt++) {
        if (!rename(path, temp)) break;
        if (!passing_linux(attempt, errno)) return why();
    }
    for (int attempt = 0;; attempt++) {
        if (!unlink(temp)) break;
        if (!passing_linux(attempt, errno)) return errno == ENOENT ? (settle_at(dir, path) ? P_OK : P_FAILED) : P_FAILED;
    }
    return settle_at(dir, path) ? P_OK : P_FAILED;
}

static p_result remove_at(int dir, const char *path) {
    for (int attempt = 0;; attempt++) {
        if (!unlinkat(dir, path, 0)) break;
        if (!passing_linux(attempt, errno)) return why();
    }
    return settle_at(dir, path) ? P_OK : P_FAILED;
}

p_result p_remove(const char *path) { return remove_at(AT_FDCWD, path); }

static p_result sync_at(int dir, const char *path) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_flush || plat_fail_sync) return P_FAILED;
#endif
    int file = -1;
    for (int attempt = 0;; attempt++) {
        file = openat(dir, path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK); /* never waits on a pipe put in its place */
        if (file >= 0 || !passing_linux(attempt, errno)) break;
    }
    if (file < 0) return why();
    struct stat info;
    bool good = !fstat(file, &info) && S_ISREG(info.st_mode) && !fsync(file);
    close(file);
    return good && settle_at(dir, path) ? P_OK : P_FAILED;
}

p_result p_sync(const char *path) { return sync_at(AT_FDCWD, path); }

static bool sync_dir(int dir, const char *path) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_flush) return false;
#endif
    int file = openat(dir, path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (file < 0) return false;
    bool good = !fsync(file);
    close(file);
    return good;
}

static p_result folder_at(int dir, const char *path) {
    struct stat info;
    if (!mkdirat(dir, path, 0700)) return settle_at(dir, path) && sync_dir(dir, path) ? P_OK : P_FAILED;
    if (errno == EEXIST && !fstatat(dir, path, &info, AT_SYMLINK_NOFOLLOW) && S_ISDIR(info.st_mode))
        return settle_at(dir, path) && sync_dir(dir, path) ? P_OK : P_FAILED;
    return errno == EEXIST ? P_FAILED : why();
}

p_result p_folder(const char *path) { return folder_at(AT_FDCWD, path); }

p_result p_real_dir(const char *path) {
    struct stat info;
    if (lstat(path, &info)) return errno == ENOENT ? P_MISSING : P_FAILED;
    return S_ISDIR(info.st_mode) ? P_OK : P_FAILED;
}

p_result p_cwd(char **path_out) {
    *path_out = NULL;
    char *buf = getcwd(NULL, 0);
    if (!buf) return P_FAILED;
    *path_out = buf;
    return P_OK;
}

static p_result list_at(int dir, const char *path, bool (*each)(void *user, const char *name), void *user) {
    int fd = openat(dir, path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
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

p_result p_list(const char *path, bool (*each)(void *user, const char *name), void *user) { return list_at(AT_FDCWD, path, each, user); }

/* A folder held by its descriptor. Each folder of rel is opened from the one before with O_NOFOLLOW, so
   a symlink anywhere below root is refused, and every name inside is then reached through the held
   descriptor, never through a path that could be swapped meanwhile. */
struct p_dir {
    int fd;
};

p_result p_dir_open(const char *root, const char *rel, p_dir **out) {
    *out = NULL;
    int at = open(root, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (at < 0) return why();
    char part[256];
    for (const char *p = rel; *p;) {
        size_t n = 0;
        while (p[n] && p[n] != '/') n++;
        if (!n || n >= sizeof part || (n == 1 && p[0] == '.') || (n == 2 && p[0] == '.' && p[1] == '.')) {
            close(at);
            return P_FAILED;
        }
        memcpy(part, p, n), part[n] = 0;
        int next = openat(at, part, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
        int err = errno;
        close(at);
        if (next < 0) {
#ifdef HELIO_TEST_HOOKS
            if (err != ENOENT) plat_last_hold_error = (unsigned long)err;
#endif
            return err == ENOENT ? P_MISSING : P_FAILED; /* ELOOP or ENOTDIR: a link or not a folder */
        }
        at = next;
        p += n;
        if (*p == '/') p++;
    }
    p_dir *dir = p_alloc(sizeof *dir);
    if (!dir) {
        close(at);
        return P_FAILED;
    }
    dir->fd = at;
    *out = dir;
    return P_OK;
}

void p_dir_close(p_dir *dir) {
    if (!dir) return;
    close(dir->fd);
    p_free(dir);
}

static bool plain_name(const char *name) { return *name && !strchr(name, '/') && strcmp(name, ".") && strcmp(name, ".."); }

p_result p_dir_read(p_dir *dir, const char *name, size_t most, char **data, size_t *length) {
    return plain_name(name) ? read_at(dir->fd, name, most, data, length) : P_FAILED;
}
p_result p_dir_write(p_dir *dir, const char *name, const char *data, size_t length, bool replace, const char *unique) {
    return plain_name(name) ? write_at(dir->fd, name, data, length, replace, unique) : P_FAILED;
}
p_result p_dir_remove(p_dir *dir, const char *name) { return plain_name(name) ? remove_at(dir->fd, name) : P_FAILED; }
p_result p_dir_sync(p_dir *dir, const char *name) { return plain_name(name) ? sync_at(dir->fd, name) : P_FAILED; }
p_result p_dir_folder(p_dir *dir, const char *name) { return plain_name(name) ? folder_at(dir->fd, name) : P_FAILED; }
p_result p_dir_list(p_dir *dir, bool (*each)(void *user, const char *name), void *user) { return list_at(dir->fd, ".", each, user); }

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

p_result p_env_checked(const char *name, char **value_out) {
    *value_out = NULL;
    const char *value = getenv(name);
    if (!value) return P_MISSING;
    *value_out = strdup(value);
    return *value_out ? P_OK : P_FAILED;
}

bool p_print(const char *text) { return fputs(text, stdout) >= 0 && !fflush(stdout); }
bool p_error(const char *text) { return fputs(text, stderr) >= 0; }
