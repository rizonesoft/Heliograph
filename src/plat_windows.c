#include <impossible/file.h>
#include <impossible/text.h>
#include <windows.h>
#include "plat.h"

static p_result from(im_result result) {
    return (p_result)result;
}
_Static_assert((int)IM_FAILED == (int)P_FAILED && (int)IM_EXISTS == (int)P_EXISTS && (int)IM_TOO_LARGE == (int)P_TOO_LARGE, "the same order");

#ifdef HELIO_TEST_HOOKS
bool plat_fail_flush = false;
bool plat_fail_write_flush = false;
bool plat_fail_sync = false;
uint64_t plat_now_override = 0;
uint64_t plat_now_step = 0; /* when set, each reading of the clock moves it on by this much, for a test that crosses midnight */
void (*plat_pause)(const char *stage) = NULL; /* called between steps of a platform call, for a test to act */
unsigned long plat_last_hold_error = 0; /* the Windows error of the last refused folder hold, for a test to report */
#endif

void *p_alloc(size_t bytes) { return im_alloc(bytes); }
void p_free(void *memory) { im_free(memory); }
p_result p_read(const char *path, size_t most, char **data, size_t *length) { return from(im_read(path, most, data, length)); }
p_result p_write(const char *path, const char *data, size_t length, bool replace, const char *unique) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_sync) return P_FAILED;
#endif
    p_result r = from(im_write(path, data, length, replace, unique));
#ifdef HELIO_TEST_HOOKS
    if (r == P_OK && plat_fail_write_flush) return P_UNFLUSHED;
#endif
    return r;
}

static bool passing_win(int attempt, DWORD error) {
    if (attempt >= 50 || (error != ERROR_SHARING_VIOLATION && error != ERROR_ACCESS_DENIED && error != ERROR_LOCK_VIOLATION)) return false;
    Sleep(4);
    return true;
}
static bool is_wsl_unc(const char *path) {
    static const char p1[] = "\\\\wsl.localhost\\";
    static const char p2[] = "\\\\wsl$\\";
    const char *prefixes[] = {p1, p2};
    for (size_t k = 0; k < 2; k++) {
        size_t i = 0;
        for (; prefixes[k][i]; i++) {
            char a = path[i], b = prefixes[k][i];
            if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
            if (a != b) break;
        }
        if (!prefixes[k][i]) return true;
    }
    return false;
}

static bool settle_win_ex(const char *path, bool allow_unc_enosys) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_flush) return false;
#endif
    size_t n = im_length(path);
    size_t slash = n;
    while (slash > 0 && path[slash - 1] != '/' && path[slash - 1] != '\\') slash--;
    const char *dir_utf8 = ".";
    char *copy = NULL;
    if (slash > 0) {
        size_t dlen = slash;
        if (dlen > 1 && !(dlen == 3 && path[1] == ':')) dlen--;
        copy = im_alloc(dlen + 1);
        if (!copy) return false;
        for (size_t i = 0; i < dlen; i++) copy[i] = path[i];
        copy[dlen] = 0;
        dir_utf8 = copy;
    }
    bool is_wsl = is_wsl_unc(dir_utf8);
    wchar_t *wide = im_wide(dir_utf8);
    im_free(copy);
    if (!wide) return false;
    HANDLE dir = CreateFileW(wide, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, NULL);
    im_free(wide);
    if (dir == INVALID_HANDLE_VALUE) return false;
    bool ok = FlushFileBuffers(dir) != 0;
    DWORD err = ok ? 0 : GetLastError();
    CloseHandle(dir);
    /* WORKAROUND: Windows 9p redirector (\\wsl.localhost\ and \\wsl$\) does not implement IRP_MJ_FLUSH_BUFFERS on
       directory handles (returning ERROR_INVALID_FUNCTION/ERROR_NOT_SUPPORTED); local NTFS and non-WSL UNC shares
       require strict FlushFileBuffers, and CreateFileW open + file-handle FlushFileBuffers remain strict everywhere. */
    return ok || (allow_unc_enosys && is_wsl && (err == ERROR_INVALID_FUNCTION || err == ERROR_NOT_SUPPORTED));
}

static bool settle_win(const char *path) { return settle_win_ex(path, true); }

p_result p_remove(const char *path) {
    wchar_t *wide = im_wide(path);
    if (!wide) return P_FAILED;
    DWORD err = 0;
    bool deleted = false;
    for (int attempt = 0;; attempt++) {
        if (DeleteFileW(wide)) {
            deleted = true;
            break;
        }
        err = GetLastError();
        if (!passing_win(attempt, err)) break;
    }
    im_free(wide);
    if (!deleted) {
        if (err == ERROR_FILE_NOT_FOUND || err == ERROR_PATH_NOT_FOUND) return P_MISSING;
        return P_FAILED;
    }
    return settle_win(path) ? P_OK : P_FAILED;
}

p_result p_create_excl(const char *path, const char *data, size_t length) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_sync) return P_FAILED;
#endif
    if (length > 0) {
        static uint64_t excl_seq = 0;
        size_t plen = im_length(path);
        size_t slash = plen;
        while (slash > 0 && path[slash - 1] != '/' && path[slash - 1] != '\\') slash--;
        wchar_t *wpath = im_wide(path);
        if (!wpath) return P_FAILED;
        /* 1. Write and flush complete payload to a short same-directory .part temp file
              (keeps temp well under MAX_PATH even for maximum-length session filenames). */
        wchar_t *wtemp = NULL;
        HANDLE tf = INVALID_HANDLE_VALUE;
        for (int attempt = 0; attempt < 32; attempt++) {
            uint64_t h = (uint64_t)(uintptr_t)&tf ^ p_tick() ^ ((++excl_seq + (uint64_t)attempt) * 2654435761u);
            h ^= h >> 33;
            h *= 0xff51afd7ed558ccdULL;
            h ^= h >> 33;
            char *temp = im_alloc(slash + 24);
            if (!temp) {
                im_free(wpath);
                return P_FAILED;
            }
            size_t p = 0;
            for (size_t i = 0; i < slash; i++) temp[p++] = path[i];
            temp[p++] = '.', temp[p++] = 'e', temp[p++] = 'x';
            static const char hx[] = "0123456789abcdef";
            for (int k = 7; k >= 0; k--) temp[p++] = hx[(h >> (k * 4)) & 0xf];
            const char *psfx = ".part";
            for (size_t i = 0; i <= 5; i++) temp[p++] = psfx[i];
            wtemp = im_wide(temp);
            im_free(temp);
            if (!wtemp) {
                im_free(wpath);
                return P_FAILED;
            }
            tf = CreateFileW(wtemp, GENERIC_WRITE, 0, NULL, CREATE_NEW, 0, NULL);
            if (tf != INVALID_HANDLE_VALUE) break;
            DWORD err = GetLastError();
            im_free(wtemp);
            wtemp = NULL;
            if (err == ERROR_FILE_EXISTS || err == ERROR_ALREADY_EXISTS) continue;
            im_free(wpath);
            return (err == ERROR_PATH_NOT_FOUND || err == ERROR_FILE_NOT_FOUND) ? P_MISSING : P_FAILED;
        }
        if (tf == INVALID_HANDLE_VALUE) {
            im_free(wpath);
            return P_FAILED;
        }
        const char *at = data;
        bool good = true;
        for (size_t left = length; good && left;) {
            DWORD part = left > 0x100000 ? 0x100000 : (DWORD)left, wrote = 0;
            good = WriteFile(tf, at, part, &wrote, NULL) && wrote;
            at += wrote, left -= wrote;
        }
        good = good && FlushFileBuffers(tf);
        CloseHandle(tf);
        if (!good) {
            DeleteFileW(wtemp);
            im_free(wtemp), im_free(wpath);
            return P_FAILED;
        }
        /* 2. Atomically publish complete flushed temp without replacing an existing destination
              (same non-replacing MoveFileExW semantics as ImpossibleSDK im_write(replace=false)). */
        p_result res = P_FAILED;
        for (int attempt = 0;; attempt++) {
            if (MoveFileExW(wtemp, wpath, MOVEFILE_WRITE_THROUGH)) {
                res = P_OK;
                break;
            }
            DWORD merr = GetLastError();
            if (merr == ERROR_ALREADY_EXISTS || merr == ERROR_FILE_EXISTS ||
                GetFileAttributesW(wpath) != INVALID_FILE_ATTRIBUTES) {
                res = P_EXISTS;
                break;
            }
            if (merr == ERROR_PATH_NOT_FOUND || merr == ERROR_FILE_NOT_FOUND) {
                res = P_MISSING;
                break;
            }
            if (!passing_win(attempt, merr)) {
                res = P_FAILED;
                break;
            }
        }
        if (res != P_OK) {
            DeleteFileW(wtemp);
            im_free(wtemp), im_free(wpath);
            return res;
        }
        im_free(wtemp), im_free(wpath);
#ifdef HELIO_TEST_HOOKS
        if (plat_fail_write_flush) return P_UNFLUSHED;
#endif
        return settle_win(path) ? P_OK : P_UNFLUSHED;
    }
    wchar_t *wide = im_wide(path);
    if (!wide) return P_FAILED;
    HANDLE file = CreateFileW(wide, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW, 0, NULL);
    DWORD err = file == INVALID_HANDLE_VALUE ? GetLastError() : 0;
    if (file == INVALID_HANDLE_VALUE) {
        im_free(wide);
        if (err == ERROR_FILE_EXISTS || err == ERROR_ALREADY_EXISTS) return P_EXISTS;
        if (err == ERROR_PATH_NOT_FOUND || err == ERROR_FILE_NOT_FOUND) return P_MISSING;
        return P_FAILED;
    }
    bool good = FlushFileBuffers(file) != 0;
    CloseHandle(file);
    if (!good) {
        DeleteFileW(wide);
        im_free(wide);
        return P_FAILED;
    }
    im_free(wide);
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_write_flush) return P_UNFLUSHED;
#endif
    return settle_win(path) ? P_OK : P_UNFLUSHED;
}

p_result p_file_mtime(const char *path, uint64_t *mtime_us) {
    wchar_t *wide = im_wide(path);
    if (!wide) return P_FAILED;
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(wide, &fd);
    DWORD err = h == INVALID_HANDLE_VALUE ? GetLastError() : 0;
    im_free(wide);
    if (h == INVALID_HANDLE_VALUE) {
        if (err == ERROR_FILE_NOT_FOUND || err == ERROR_PATH_NOT_FOUND) return P_MISSING;
        return P_FAILED;
    }
    FindClose(h);
    uint64_t ticks = ((uint64_t)fd.ftLastWriteTime.dwHighDateTime << 32) | (uint64_t)fd.ftLastWriteTime.dwLowDateTime;
    uint64_t us = ticks / 10ull;
    if (us < 11644473600000000ull) return P_FAILED;
    *mtime_us = us - 11644473600000000ull;
    return P_OK;
}

p_result p_claim_remove(const char *path, const char *unique) {
    im_text temp_t = {0};
    im_add(&temp_t, path), im_add(&temp_t, "."), im_add(&temp_t, unique), im_add(&temp_t, ".rm.part");
    wchar_t *from = im_wide(path);
    wchar_t *to = temp_t.failed ? NULL : im_wide(temp_t.data);
    im_text_free(&temp_t);
    if (!from || !to) {
        im_free(from), im_free(to);
        return P_FAILED;
    }
    DWORD err = 0;
    bool moved = false;
    for (int attempt = 0;; attempt++) {
        if (MoveFileExW(from, to, MOVEFILE_WRITE_THROUGH)) {
            moved = true;
            break;
        }
        err = GetLastError();
        if (!passing_win(attempt, err)) break;
    }
    im_free(from);
    if (!moved) {
        im_free(to);
        if (err == ERROR_FILE_NOT_FOUND || err == ERROR_PATH_NOT_FOUND) return P_MISSING;
        return P_FAILED;
    }
    bool deleted = false;
    for (int attempt = 0;; attempt++) {
        if (DeleteFileW(to)) {
            deleted = true;
            break;
        }
        err = GetLastError();
        if (!passing_win(attempt, err)) break;
    }
    im_free(to);
    if (!deleted && err != ERROR_FILE_NOT_FOUND && err != ERROR_PATH_NOT_FOUND) return P_FAILED;
    return settle_win(path) ? P_OK : P_FAILED;
}

static bool sync_dir_win(const char *path, bool allow_unc_enosys) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_flush) return false;
#endif
    bool is_wsl = is_wsl_unc(path);
    wchar_t *wide = im_wide(path);
    if (!wide) return false;
    HANDLE dir = CreateFileW(wide, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, NULL);
    im_free(wide);
    if (dir == INVALID_HANDLE_VALUE) return false;
    bool ok = FlushFileBuffers(dir) != 0;
    DWORD err = ok ? 0 : GetLastError();
    CloseHandle(dir);
    return ok || (allow_unc_enosys && is_wsl && (err == ERROR_INVALID_FUNCTION || err == ERROR_NOT_SUPPORTED));
}

p_result p_sync(const char *path) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_flush || plat_fail_sync) return P_FAILED;
#endif
    wchar_t *wide = im_wide(path);
    if (!wide) return P_FAILED;
    HANDLE file = CreateFileW(wide, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, 0, NULL);
    DWORD open_err = file == INVALID_HANDLE_VALUE ? GetLastError() : 0;
    im_free(wide);
    if (file == INVALID_HANDLE_VALUE) {
        if (open_err == ERROR_FILE_NOT_FOUND || open_err == ERROR_PATH_NOT_FOUND) return P_MISSING;
        return P_FAILED;
    }
    bool ok = FlushFileBuffers(file) != 0;
    CloseHandle(file);
    return ok && settle_win(path) ? P_OK : P_FAILED;
}

p_result p_folder(const char *path) {
    p_result r = from(im_make_folder(path));
    if (r != P_OK) return r;
    wchar_t *wide = im_wide(path);
    if (!wide) return P_FAILED;
    DWORD attrs = GetFileAttributesW(wide);
    im_free(wide);
    if (attrs == INVALID_FILE_ATTRIBUTES || !(attrs & FILE_ATTRIBUTE_DIRECTORY) || (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
        return P_FAILED;
    /* Flush parent and new directories strictly on local NTFS and non-WSL UNC shares; on WSL 9p shares
       (\\wsl.localhost\ and \\wsl$\), settle_win_ex and sync_dir_win allow ENOSYS on directory handles only. */
    return settle_win_ex(path, true) && sync_dir_win(path, true) ? P_OK : P_FAILED;
}

p_result p_real_dir(const char *path) {
    wchar_t *wide = im_wide(path);
    if (!wide) return P_FAILED;
    DWORD attrs = GetFileAttributesW(wide);
    DWORD err = attrs == INVALID_FILE_ATTRIBUTES ? GetLastError() : 0;
    im_free(wide);
    if (attrs == INVALID_FILE_ATTRIBUTES) return err == ERROR_FILE_NOT_FOUND || err == ERROR_PATH_NOT_FOUND ? P_MISSING : P_FAILED;
    return (attrs & FILE_ATTRIBUTE_DIRECTORY) && !(attrs & FILE_ATTRIBUTE_REPARSE_POINT) ? P_OK : P_FAILED;
}

p_result p_cwd(char **path_out) {
    *path_out = NULL;
    DWORD need = GetCurrentDirectoryW(0, NULL);
    if (!need) return P_FAILED;
    wchar_t *wide = im_alloc((size_t)need * sizeof *wide);
    if (!wide) return P_FAILED;
    DWORD got = GetCurrentDirectoryW(need, wide);
    if (!got || got >= need) {
        im_free(wide);
        return P_FAILED;
    }
    *path_out = im_utf8(wide);
    im_free(wide);
    return *path_out ? P_OK : P_FAILED;
}
p_result p_input(size_t most, char **data, size_t *length) { return from(im_read_input(most, data, length)); }
uint64_t p_now(void) {
#ifdef HELIO_TEST_HOOKS
    if (plat_now_override) {
        uint64_t reading = plat_now_override;
        plat_now_override += plat_now_step;
        return reading;
    }
#endif
    return im_now();
}
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

p_result p_env_checked(const char *name, char **value_out) {
    *value_out = NULL;
    wchar_t *wide = im_wide(name);
    if (!wide) return P_FAILED;
    DWORD need = GetEnvironmentVariableW(wide, NULL, 0);
    if (!need) {
        DWORD err = GetLastError();
        im_free(wide);
        return err == ERROR_ENVVAR_NOT_FOUND ? P_MISSING : P_FAILED;
    }
    wchar_t *text = im_alloc((size_t)need * sizeof *text);
    if (!text) {
        im_free(wide);
        return P_FAILED;
    }
    p_result r = P_FAILED;
    if (GetEnvironmentVariableW(wide, text, need) < need) {
        *value_out = im_utf8(text);
        if (*value_out) r = P_OK;
    }
    im_free(text);
    im_free(wide);
    return r;
}

char *p_env(const char *name) {
    char *value = NULL;
    return p_env_checked(name, &value) == P_OK ? value : NULL;
}

bool p_error(const char *text) {
    HANDLE out = GetStdHandle(STD_ERROR_HANDLE);
    DWORD done;
    return out && out != INVALID_HANDLE_VALUE && WriteFile(out, text, (DWORD)im_length(text), &done, NULL);
}

/* A folder held by handles on each folder of its path from the root. Each is opened as itself (never through a
   reparse point) and without FILE_SHARE_DELETE, so on NTFS none can be renamed or removed while held, and each
   is refused when it is a reparse point. Names inside are then reached through that unchangeable path; a file is
   opened as itself too and refused when it is a reparse point. Over \\wsl.localhost a Linux symlink is a reparse
   point Windows cannot traverse, so it is refused the same way. */
enum { MOST_HELD = 4 };
struct p_dir {
    char *path;
    HANDLE held[MOST_HELD];
    int count;
};

static p_result hold_folder(const char *path, HANDLE *out) {
    wchar_t *wide = im_wide(path);
    if (!wide) return P_FAILED;
    HANDLE h = INVALID_HANDLE_VALUE;
    DWORD err = 0;
    for (int attempt = 0;; attempt++) {
        h = CreateFileW(wide, FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING,
                        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
        if (h != INVALID_HANDLE_VALUE) break;
        err = GetLastError();
        if (!passing_win(attempt, err)) break;
    }
    if (h == INVALID_HANDLE_VALUE) {
        im_free(wide);
#ifdef HELIO_TEST_HOOKS
        if (err != ERROR_FILE_NOT_FOUND && err != ERROR_PATH_NOT_FOUND) plat_last_hold_error = err;
#endif
        return err == ERROR_FILE_NOT_FOUND || err == ERROR_PATH_NOT_FOUND ? P_MISSING : P_FAILED;
    }
    DWORD attrs = GetFileAttributesW(wide); /* the held folder: its name cannot change while h is open */
#ifdef HELIO_TEST_HOOKS
    DWORD aerr = attrs == INVALID_FILE_ATTRIBUTES ? GetLastError() : 0;
#endif
    im_free(wide);
    if (attrs == INVALID_FILE_ATTRIBUTES || !(attrs & FILE_ATTRIBUTE_DIRECTORY) || (attrs & FILE_ATTRIBUTE_REPARSE_POINT)) {
#ifdef HELIO_TEST_HOOKS
        plat_last_hold_error = aerr ? aerr : 0x10000u | (attrs & 0xffffu);
#endif
        CloseHandle(h);
        return P_FAILED;
    }
    *out = h;
    return P_OK;
}

void p_dir_close(p_dir *dir) {
    if (!dir) return;
    for (int i = dir->count; i-- > 0;) CloseHandle(dir->held[i]);
    im_free(dir->path);
    im_free(dir);
}

p_result p_dir_open(const char *root, const char *rel, p_dir **out) {
    *out = NULL;
    p_dir *dir = im_alloc(sizeof *dir);
    if (!dir) return P_FAILED;
    im_text path = {0};
    im_add(&path, root);
    p_result r = path.failed ? P_FAILED : P_OK;
    for (const char *p = rel; r == P_OK && *p;) {
        size_t n = 0;
        while (p[n] && p[n] != '/' && p[n] != '\\') n++;
        if (!n || dir->count == MOST_HELD || (n == 1 && p[0] == '.') || (n == 2 && p[0] == '.' && p[1] == '.')) {
            r = P_FAILED;
            break;
        }
        im_add(&path, "/"), im_add_bytes(&path, p, n);
        if (path.failed) {
            r = P_FAILED;
            break;
        }
        r = hold_folder(path.data, &dir->held[dir->count]);
        if (r == P_OK) dir->count++;
        p += n;
        if (*p) p++;
    }
    if (r != P_OK) {
        im_text_free(&path);
        p_dir_close(dir);
        return r;
    }
    dir->path = path.data;
    *out = dir;
    return P_OK;
}

static bool plain_name(const char *name) {
    if (!*name || (name[0] == '.' && (!name[1] || (name[1] == '.' && !name[2])))) return false;
    for (const char *p = name; *p; p++)
        if (*p == '/' || *p == '\\' || *p == ':') return false;
    return true;
}

static char *inside(const p_dir *dir, const char *name) {
    if (!plain_name(name)) return NULL;
    im_text t = {0};
    im_add(&t, dir->path), im_add(&t, "/"), im_add(&t, name);
    if (t.failed) {
        im_text_free(&t);
        return NULL;
    }
    return t.data;
}

/* Opens a file inside a held folder as itself, without FILE_SHARE_DELETE so its name stays put while it is checked,
   and refuses a reparse point or a folder in its place. */
static p_result open_inside(const char *path, DWORD access, HANDLE *out) {
    wchar_t *wide = im_wide(path);
    if (!wide) return P_FAILED;
    HANDLE h = INVALID_HANDLE_VALUE;
    DWORD err = 0;
    for (int attempt = 0;; attempt++) {
        h = CreateFileW(wide, access, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, NULL);
        if (h != INVALID_HANDLE_VALUE) break;
        err = GetLastError();
        if (!passing_win(attempt, err)) break;
    }
    if (h == INVALID_HANDLE_VALUE) {
        im_free(wide);
        return err == ERROR_FILE_NOT_FOUND || err == ERROR_PATH_NOT_FOUND ? P_MISSING : P_FAILED;
    }
    DWORD attrs = GetFileAttributesW(wide);
    im_free(wide);
    if (attrs == INVALID_FILE_ATTRIBUTES || (attrs & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT))) {
        CloseHandle(h);
        return P_FAILED;
    }
    *out = h;
    return P_OK;
}

p_result p_dir_read(p_dir *dir, const char *name, size_t most, char **data, size_t *length) {
    char *path = inside(dir, name);
    if (!path) return P_FAILED;
    HANDLE h;
    p_result r = open_inside(path, GENERIC_READ, &h);
    im_free(path);
    if (r != P_OK) return r;
    im_text text = {0};
    char part[4096];
    DWORD got = 0;
    for (r = P_OK; r == P_OK;) {
        if (!ReadFile(h, part, sizeof part, &got, NULL)) r = P_FAILED;
        else if (!got) break;
        else if (got > most - text.length) r = P_TOO_LARGE;
        else if (im_add_bytes(&text, part, got), text.failed) r = P_FAILED;
    }
    CloseHandle(h);
    if (r == P_OK && !text.data) im_add_bytes(&text, "", 0);
    if (r == P_OK && text.failed) r = P_FAILED;
    if (r != P_OK) {
        im_text_free(&text);
        return r;
    }
    *data = text.data, *length = text.length;
    return P_OK;
}

p_result p_dir_write(p_dir *dir, const char *name, const char *data, size_t length, bool replace, const char *unique) {
    char *path = inside(dir, name);
    if (!path) return P_FAILED;
    p_result r = p_write(path, data, length, replace, unique); /* a new temporary file moved onto the name: never through a link */
    im_free(path);
    return r;
}

p_result p_dir_remove(p_dir *dir, const char *name) {
    char *path = inside(dir, name);
    if (!path) return P_FAILED;
    p_result r = p_remove(path); /* removes a link in its place, never its target */
    im_free(path);
    return r;
}

p_result p_dir_sync(p_dir *dir, const char *name) {
#ifdef HELIO_TEST_HOOKS
    if (plat_fail_flush || plat_fail_sync) return P_FAILED;
#endif
    char *path = inside(dir, name);
    if (!path) return P_FAILED;
    HANDLE h;
    p_result r = open_inside(path, GENERIC_WRITE, &h);
    if (r == P_OK) {
        bool ok = FlushFileBuffers(h) != 0;
        CloseHandle(h);
        r = ok && settle_win(path) ? P_OK : P_FAILED;
    }
    im_free(path);
    return r;
}

/* Makes a folder inside a held one and flushes it through its own handle, opened as itself and without
   FILE_SHARE_DELETE, so a junction put in its place after it was made is refused, never flushed through. */
p_result p_dir_folder(p_dir *dir, const char *name) {
    char *path = inside(dir, name);
    if (!path) return P_FAILED;
    p_result r = from(im_make_folder(path));
#ifdef HELIO_TEST_HOOKS
    if (r == P_OK && plat_pause) plat_pause("after-make-folder");
#endif
    wchar_t *wide = r == P_OK ? im_wide(path) : NULL;
    if (r == P_OK && !wide) r = P_FAILED;
    HANDLE h = INVALID_HANDLE_VALUE;
    DWORD err = 0;
    for (int attempt = 0; r == P_OK; attempt++) {
        h = CreateFileW(wide, GENERIC_WRITE | FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                        OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
        if (h != INVALID_HANDLE_VALUE) break;
        err = GetLastError();
        if (!passing_win(attempt, err)) r = P_FAILED;
    }
    if (r == P_OK) {
        DWORD attrs = GetFileAttributesW(wide); /* the folder h holds: its name cannot change while h is open */
        if (attrs == INVALID_FILE_ATTRIBUTES || !(attrs & FILE_ATTRIBUTE_DIRECTORY) || (attrs & FILE_ATTRIBUTE_REPARSE_POINT)) r = P_FAILED;
    }
#ifdef HELIO_TEST_HOOKS
    if (r == P_OK && plat_fail_flush) r = P_FAILED;
#endif
    if (r == P_OK && !FlushFileBuffers(h)) {
        DWORD ferr = GetLastError();
        /* WORKAROUND: as in settle_win_ex, the 9p redirector has no flush for a folder handle. */
        if (!(is_wsl_unc(path) && (ferr == ERROR_INVALID_FUNCTION || ferr == ERROR_NOT_SUPPORTED))) r = P_FAILED;
    }
    if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
    im_free(wide);
    if (r == P_OK && !settle_win(path)) r = P_FAILED; /* the parent: held, so its name cannot change */
    im_free(path);
    return r;
}

p_result p_dir_list(p_dir *dir, bool (*each)(void *user, const char *name), void *user) { return p_list(dir->path, each, user); }
