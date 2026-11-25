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

bool p_is_real_dir(const char *path) {
    wchar_t *wide = im_wide(path);
    if (!wide) return false;
    DWORD attrs = GetFileAttributesW(wide);
    im_free(wide);
    return attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_DIRECTORY) && !(attrs & FILE_ATTRIBUTE_REPARSE_POINT);
}

p_result p_at_root(const char *dir, bool *at_root) {
    *at_root = false;
    wchar_t *wide = im_wide(dir);
    if (!wide) return P_FAILED;
    DWORD attrs = GetFileAttributesW(wide);
    im_free(wide);
    if (attrs == INVALID_FILE_ATTRIBUTES || !(attrs & FILE_ATTRIBUTE_DIRECTORY)) return P_FAILED;
    /* Keep relative "../" chains within Windows MAX_PATH (260): 80 steps is 240 chars (+ "helio.conf" = 250 < 260). */
    size_t ups = 0;
    for (const char *p = dir; p[0] == '.' && p[1] == '.' && (p[2] == '/' || p[2] == '\\' || !p[2]); p += p[2] ? 3 : 2) ups++;
    if (ups >= 80) *at_root = true;
    return P_OK;
}
p_result p_input(size_t most, char **data, size_t *length) { return from(im_read_input(most, data, length)); }
uint64_t p_now(void) {
#ifdef HELIO_TEST_HOOKS
    if (plat_now_override) return plat_now_override;
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
