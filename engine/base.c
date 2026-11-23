#include "base.h"

size_t len(const char *s) {
    size_t n = 0;
    while (s[n]) n++;
    return n;
}

bool same(const char *a, const char *b) { return !compare(a, b); }

int compare(const char *a, const char *b) {
    while (*a && *a == *b) a++, b++;
    return (unsigned char)*a - (unsigned char)*b;
}

bool starts(const char *s, const char *with) {
    while (*with) if (*s++ != *with++) return false;
    return true;
}

void add_bytes(text *t, const char *more, size_t bytes) {
    if (t->failed) return;
    if (bytes > ((size_t)1 << 30) - t->length) {
        t->failed = true;
        return;
    }
    if (t->length + bytes + 1 > t->capacity) {
        size_t capacity = (t->length + bytes + 1) * 2 + 64;
        char *data = p_alloc(capacity);
        if (!data) {
            t->failed = true;
            return;
        }
        if (t->data) memcpy(data, t->data, t->length);
        p_free(t->data);
        t->data = data, t->capacity = capacity;
    }
    memcpy(t->data + t->length, more, bytes);
    t->length += bytes;
    t->data[t->length] = 0;
}

void add(text *t, const char *more) { add_bytes(t, more, len(more)); }

void add_number(text *t, uint64_t value, int digits) {
    char out[24];
    int at = sizeof out;
    do out[--at] = (char)('0' + value % 10), value /= 10, digits--;
    while ((value || digits > 0) && at);
    add_bytes(t, out + at, sizeof out - (size_t)at);
}

void text_free(text *t) {
    p_free(t->data);
    *t = (text){0};
}

char *copy(const char *s, size_t length) {
    char *out = p_alloc(length + 1);
    if (out) memcpy(out, s, length);
    return out;
}

void add_time(text *t, uint64_t microseconds) {
    uint64_t seconds = microseconds / 1000000, days = seconds / 86400, rest = seconds % 86400;
    /* Days to a civil date, from a year that starts in March. */
    uint64_t z = days + 719468, era = z / 146097, doe = z - era * 146097;
    uint64_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365, doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    uint64_t mp = (5 * doy + 2) / 153, month = mp < 10 ? mp + 3 : mp - 9;
    add_number(t, yoe + era * 400 + (month <= 2), 4), add(t, "-"), add_number(t, month, 2), add(t, "-");
    add_number(t, doy - (153 * mp + 2) / 5 + 1, 2), add(t, "T"), add_number(t, rest / 3600, 2), add(t, ":");
    add_number(t, rest / 60 % 60, 2), add(t, ":"), add_number(t, rest % 60, 2), add(t, "Z");
}
