#pragma once
/* The whole program: the exit code is 0 done, 1 nothing, 2 refused, 3 damaged. */
int helio(int count, char **args);
int helio_paused(int count, char **args, void (*pause)(void *user, const char *stage), void *user);
