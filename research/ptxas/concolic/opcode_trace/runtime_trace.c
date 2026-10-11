/* Host-side LD_PRELOAD shim for SymQEMU, not a target-side ptxas preload.
 * Forward every call unchanged. PATH sites are guest translation-block PCs,
 * so use native disassembly to identify the exact instruction within the block.
 * SymCC's expression renderer truncates long expressions at 4095 characters.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
static FILE *logstream(void) {
    static FILE *stream;
    if (!stream) {
        const char *path = getenv("PTX_TRACE_LOG");
        stream = path ? fopen(path, "w") : stderr;
        if (!stream) {
            perror("PTX_TRACE_LOG");
            abort();
        }
    }
    return stream;
}

static const char *render(void *expression) {
    static const char *(*real)(void *);
    if (!real) real = dlsym(RTLD_NEXT, "_sym_expr_to_string");
    return real(expression);
}

void _sym_push_path_constraint(void *expression, int taken, uintptr_t site) {
    static void (*real)(void *, int, uintptr_t);
    if (!real) real = dlsym(RTLD_NEXT, "_sym_push_path_constraint");
    if (expression) {
        fprintf(logstream(), "PATH %lx %d %s\n", (unsigned long)site,
                taken, render(expression));
        fflush(logstream());
    }
    real(expression, taken, site);
}

void *_sym_read_memory(uint8_t *address, size_t length, _Bool little_endian) {
    static void *(*real)(uint8_t *, size_t, _Bool);
    if (!real) real = dlsym(RTLD_NEXT, "_sym_read_memory");
    void *expression = real(address, length, little_endian);
    if (length == 1 && expression)
        fprintf(logstream(), "BYTE %p %02x %s\n", (void *)address, *address, render(expression));
    if (length >= 3 && length <= 8 && !memcmp(address, "add", 3))
        fprintf(logstream(), "ADD %p %zu %s\n", (void *)address, length,
                expression ? render(expression) : "CONCRETE");
    return expression;
}
