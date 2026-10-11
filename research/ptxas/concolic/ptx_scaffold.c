/* AFL++ custom mutator whose only job is post-processing: the queue holds just the instruction
   text, and every execution gets PTX_PREFIX ++ input ++ PTX_SUFFIX, so mutation never touches the scaffold.
   cc -O2 -shared -fPIC -o ptx_scaffold.so ptx_scaffold.c */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { unsigned char *prefix, *suffix, *out; size_t prefix_len, suffix_len, cap; } scaffold_t;

static unsigned char *slurp(const char *path, size_t *len) {
  FILE *f = path ? fopen(path, "rb") : NULL;
  if (!f) { fprintf(stderr, "ptx_scaffold: cannot read %s\n", path ? path : "(unset)"); exit(1); }
  fseek(f, 0, SEEK_END); *len = ftell(f); rewind(f);
  unsigned char *data = malloc(*len + 1);
  if (fread(data, 1, *len, f) != *len) exit(1);
  fclose(f);
  return data;
}

void *afl_custom_init(void *afl, unsigned int seed) {
  (void)afl; (void)seed;
  scaffold_t *s = calloc(1, sizeof *s);
  s->prefix = slurp(getenv("PTX_PREFIX"), &s->prefix_len);
  s->suffix = slurp(getenv("PTX_SUFFIX"), &s->suffix_len);
  s->cap = s->prefix_len + s->suffix_len + (1 << 20);
  s->out = malloc(s->cap);
  return s;
}

unsigned int afl_custom_fuzz_count(void *data, const unsigned char *buf, size_t size) { (void)data; (void)buf; (void)size; return 0; }

size_t afl_custom_fuzz(void *data, unsigned char *buf, size_t size, unsigned char **out, unsigned char *add, size_t add_size, size_t max_size) {
  (void)data; (void)buf; (void)size; (void)add; (void)add_size; (void)max_size;
  *out = buf; return 0;
}

size_t afl_custom_post_process(void *data, unsigned char *buf, size_t size, unsigned char **out) {
  scaffold_t *s = data;
  size_t total = s->prefix_len + size + s->suffix_len;
  if (total > s->cap) { s->cap = total; s->out = realloc(s->out, s->cap); }
  memcpy(s->out, s->prefix, s->prefix_len);
  memcpy(s->out + s->prefix_len, buf, size);
  memcpy(s->out + s->prefix_len + size, s->suffix, s->suffix_len);
  *out = s->out;
  return total;
}

void afl_custom_deinit(void *data) { (void)data; }
