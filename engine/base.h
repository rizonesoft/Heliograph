/* Text helpers, so the core needs no C library. */
#pragma once
#include "plat.h"

typedef struct { char *data; size_t length, capacity; bool failed; } text;
void add(text *t, const char *more);
void add_bytes(text *t, const char *more, size_t bytes);
void add_number(text *t, uint64_t value, int digits);
void text_free(text *t);
size_t len(const char *s);
bool same(const char *a, const char *b);
bool starts(const char *s, const char *with);
int compare(const char *a, const char *b);
char *copy(const char *s, size_t length);
/* "2026-11-23T15:17:28Z" */
void add_time(text *t, uint64_t microseconds);
/* The opposite, or false when it is not such a time. */
bool read_time(const char *t, uint64_t *microseconds);
/* 64 lowercase hex characters of SHA-256 over data[0..length-1]. */
void add_sha256_hex(text *t, const char *data, size_t length);
void *memcpy(void *restrict to, const void *restrict from, size_t bytes);
