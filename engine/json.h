/* JSON written into a text: strings escaped, and any byte sequence that is not UTF-8 replaced by U+FFFD, so a
   script always reads valid JSON in UTF-8. No floating point and no system call. */
#pragma once
#include "base.h"

/* A JSON string of the n bytes at s (s may hold any bytes). */
void json_string(text *out, const char *s, size_t n);
/* A JSON string of s, or null when s is NULL. */
void json_text(text *out, const char *s);

/* An object or array being written; first is true until its first member. */
typedef struct {
    text *out;
    bool first;
} json_list;

json_list json_object(text *out);
json_list json_array(text *out);
void json_object_end(json_list *o);
void json_array_end(json_list *a);
/* Starts the member key of an object (the value follows), or the next item of an array (key NULL). */
void json_key(json_list *o, const char *key);
void json_put_text(json_list *o, const char *key, const char *value); /* null when value is NULL */
void json_put_bytes(json_list *o, const char *key, const char *value, size_t n);
void json_put_number(json_list *o, const char *key, uint64_t value);
void json_put_bool(json_list *o, const char *key, bool value);
void json_put_null(json_list *o, const char *key);
