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

bool read_time(const char *t, uint64_t *microseconds) {
    static const char shape[] = "0000-00-00T00:00:00Z";
    uint64_t part[6] = {0};
    int at = 0;
    for (int i = 0; shape[i]; i++) {
        if (shape[i] == '0') {
            if (t[i] < '0' || t[i] > '9') return false;
            part[at] = part[at] * 10 + (uint64_t)(t[i] - '0');
        } else if (t[i] != shape[i]) {
            return false;
        } else {
            at++;
        }
    }
    uint64_t y = part[0], m = part[1], d = part[2];
    if (t[20] || y < 1970 || m < 1 || m > 12 || d < 1 || d > 31 || part[3] > 23 || part[4] > 59 || part[5] > 60) return false;
    y -= m <= 2;
    uint64_t era = y / 400, yoe = y - era * 400, doy = (153 * (m > 2 ? m - 3 : m + 9) + 2) / 5 + d - 1;
    uint64_t days = era * 146097 + yoe * 365 + yoe / 4 - yoe / 100 + doy - 719468;
    *microseconds = (days * 86400 + part[3] * 3600 + part[4] * 60 + part[5]) * 1000000;
    return true;
}

static uint32_t rotr32(uint32_t x, unsigned n) { return (x >> n) | (x << (32 - n)); }

static void sha256_block(uint32_t h[8], const unsigned char b[64]) {
    static const uint32_t k[64] = {
        0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
        0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
        0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
        0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
        0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
        0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
        0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
        0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u, 0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u
    };
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = ((uint32_t)b[i * 4] << 24) | ((uint32_t)b[i * 4 + 1] << 16) | ((uint32_t)b[i * 4 + 2] << 8) | (uint32_t)b[i * 4 + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = rotr32(w[i - 15], 7) ^ rotr32(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = rotr32(w[i - 2], 17) ^ rotr32(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = h[0], bb = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t s1 = rotr32(e, 6) ^ rotr32(e, 11) ^ rotr32(e, 25);
        uint32_t ch = (e & f) ^ ((~e) & g);
        uint32_t t1 = hh + s1 + ch + k[i] + w[i];
        uint32_t s0 = rotr32(a, 2) ^ rotr32(a, 13) ^ rotr32(a, 22);
        uint32_t maj = (a & bb) ^ (a & c) ^ (bb & c);
        uint32_t t2 = s0 + maj;
        hh = g, g = f, f = e, e = d + t1, d = c, c = bb, bb = a, a = t1 + t2;
    }
    h[0] += a, h[1] += bb, h[2] += c, h[3] += d, h[4] += e, h[5] += f, h[6] += g, h[7] += hh;
}

void add_sha256_hex(text *t, const char *data, size_t length) {
    uint32_t h[8] = {
        0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
        0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u
    };
    const unsigned char *p = (const unsigned char *)(data ? data : "");
    size_t off = 0;
    while (length - off >= 64) {
        sha256_block(h, p + off);
        off += 64;
    }
    unsigned char tail[128];
    for (size_t i = 0; i < sizeof tail; i++) tail[i] = 0;
    size_t rem = length - off;
    for (size_t i = 0; i < rem; i++) tail[i] = p[off + i];
    tail[rem] = 0x80u;
    size_t pad_len = rem < 56 ? 64 : 128;
    uint64_t bits = (uint64_t)length * 8ull;
    for (int i = 0; i < 8; i++) tail[pad_len - 1 - (size_t)i] = (unsigned char)(bits >> (i * 8));
    sha256_block(h, tail);
    if (pad_len == 128) sha256_block(h, tail + 64);
    static const char hex[] = "0123456789abcdef";
    char out[64];
    for (int i = 0; i < 8; i++)
        for (int j = 0; j < 8; j++)
            out[i * 8 + j] = hex[(h[i] >> ((7 - j) * 4)) & 15u];
    add_bytes(t, out, 64);
}
