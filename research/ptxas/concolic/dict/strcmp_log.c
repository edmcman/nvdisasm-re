// LD_PRELOAD shim: append both operands of ptxas string comparisons to $STRCMP_LOG.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void record(const char *a, const char *b, size_t n) {
    static FILE *out;
    if (!out && !(out = fopen(getenv("STRCMP_LOG"), "a"))) return;
    fprintf(out, "%.*s\n%.*s\n", (int)strnlen(a, n), a, (int)strnlen(b, n), b);
}
#define REAL(name) static __typeof__(name) *real; if (!real) real = dlsym(RTLD_NEXT, #name)
int strcmp(const char *a, const char *b) {
    REAL(strcmp);
    record(a, b, 256); return real(a, b);
}
int strncmp(const char *a, const char *b, size_t n) {
    REAL(strncmp);
    record(a, b, n < 256 ? n : 256); return real(a, b, n);
}
