#include "json.h"

static const char HEX[] = "0123456789abcdef";
static const char REPLACEMENT[] = "\xef\xbf\xbd"; /* U+FFFD */

/* The length of the well-formed UTF-8 sequence at s (n bytes left), or 0 when it is not one: a stray continuation
   byte, a cut-short sequence, an overlong form, a surrogate or a code point past U+10FFFF. */
static size_t utf8_length(const unsigned char *s, size_t n) {
    unsigned char c = s[0];
    if (c < 0x80) return 1;
    size_t want = c >= 0xc2 && c <= 0xdf ? 2 : c >= 0xe0 && c <= 0xef ? 3 : c >= 0xf0 && c <= 0xf4 ? 4 : 0;
    if (!want || n < want) return 0;
    for (size_t i = 1; i < want; i++)
        if ((s[i] & 0xc0) != 0x80) return 0;
    if (c == 0xe0 && s[1] < 0xa0) return 0;  /* overlong */
    if (c == 0xed && s[1] > 0x9f) return 0;  /* surrogate */
    if (c == 0xf0 && s[1] < 0x90) return 0;  /* overlong */
    if (c == 0xf4 && s[1] > 0x8f) return 0;  /* past U+10FFFF */
    return want;
}

/* Bytes that cannot start a valid sequence there are skipped one maximal ill-formed run at a time, as Python's
   errors="replace" and the Unicode standard's recommended practice do: a lead byte and the continuation bytes that
   could still have belonged to it give one U+FFFD. */
static size_t bad_run(const unsigned char *s, size_t n) {
    unsigned char c = s[0];
    size_t want = c >= 0xc2 && c <= 0xdf ? 2 : c >= 0xe0 && c <= 0xef ? 3 : c >= 0xf0 && c <= 0xf4 ? 4 : 0;
    if (!want) return 1;
    size_t i = 1;
    for (; i < want && i < n; i++) {
        unsigned char lo = 0x80, hi = 0xbf;
        if (i == 1) {
            if (c == 0xe0) lo = 0xa0;
            else if (c == 0xed) hi = 0x9f;
            else if (c == 0xf0) lo = 0x90;
            else if (c == 0xf4) hi = 0x8f;
        }
        if (s[i] < lo || s[i] > hi) break;
    }
    return i;
}

void json_string(text *out, const char *s, size_t n) {
    const unsigned char *p = (const unsigned char *)s;
    add_bytes(out, "\"", 1);
    for (size_t i = 0; i < n;) {
        unsigned char c = p[i];
        if (c == '"') add_bytes(out, "\\\"", 2), i++;
        else if (c == '\\') add_bytes(out, "\\\\", 2), i++;
        else if (c == '\n') add_bytes(out, "\\n", 2), i++;
        else if (c == '\r') add_bytes(out, "\\r", 2), i++;
        else if (c == '\t') add_bytes(out, "\\t", 2), i++;
        else if (c < 0x20 || c == 0x7f) {
            char esc[6] = {'\\', 'u', '0', '0', HEX[c >> 4], HEX[c & 15]};
            add_bytes(out, esc, 6), i++;
        } else {
            size_t k = utf8_length(p + i, n - i);
            if (k) add_bytes(out, s + i, k), i += k;
            else add_bytes(out, REPLACEMENT, 3), i += bad_run(p + i, n - i);
        }
    }
    add_bytes(out, "\"", 1);
}

void json_text(text *out, const char *s) {
    if (s) json_string(out, s, len(s));
    else add(out, "null");
}

json_list json_object(text *out) {
    add(out, "{");
    return (json_list){out, true};
}

json_list json_array(text *out) {
    add(out, "[");
    return (json_list){out, true};
}

void json_object_end(json_list *o) { add(o->out, "}"); }
void json_array_end(json_list *a) { add(a->out, "]"); }

void json_key(json_list *o, const char *key) {
    if (!o->first) add(o->out, ",");
    o->first = false;
    if (key) json_text(o->out, key), add(o->out, ":");
}

void json_put_text(json_list *o, const char *key, const char *value) { json_key(o, key), json_text(o->out, value); }
void json_put_bytes(json_list *o, const char *key, const char *value, size_t n) { json_key(o, key), json_string(o->out, value, n); }
void json_put_number(json_list *o, const char *key, uint64_t value) { json_key(o, key), add_number(o->out, value, 0); }
void json_put_bool(json_list *o, const char *key, bool value) { json_key(o, key), add(o->out, value ? "true" : "false"); }
void json_put_null(json_list *o, const char *key) { json_key(o, key), add(o->out, "null"); }
