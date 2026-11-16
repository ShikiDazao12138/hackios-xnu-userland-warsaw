/*
 * apfs-tool do IMAGE [SCRIPT]: run the kernel's writing code (apfs_write.c) on an image, one
 * operation per line of SCRIPT (standard input when absent), all in one transaction until a
 * `commit` line; whatever is left is committed at the end.  Each line prints one result line,
 * which the tests read back (write-tests.py, test_wfs_random.py).
 *
 *   mkdir PATH MODE            create PATH MODE        symlink PATH TARGET
 *   mknod PATH MODE RDEV       link EXISTING NEW       unlink PATH      rmdir PATH
 *   orphan PATH                (unlink while still open)                reap ID
 *   rename FROM TO [swap|excl] truncate PATH SIZE     clone FROM TO
 *   write PATH OFFSET SEED LEN (LEN bytes of the generator below, seeded SEED)
 *   chmod PATH MODE            chown PATH UID GID      chflags PATH FLAGS
 *   utimes PATH ATIME MTIME    (nanoseconds)
 *   setxattr PATH NAME SEED LEN                        rmxattr PATH NAME
 *   docid PATH                 commit                  reap-orphans
 *
 * Numbers are C literals (0755, 0x10).  The generator is xorshift64* over the seed, eight
 * bytes a step, little-endian; test_wfs_random.py has the same one.
 */

#include "apfs_tool.h"

static void
gen_bytes(uint64_t seed, uint8_t *out, size_t len)
{
	uint64_t x = seed ? seed : 0x9E3779B97F4A7C15ULL;

	for (size_t i = 0; i < len; i += 8) {
		uint64_t v;

		x ^= x >> 12;
		x ^= x << 25;
		x ^= x >> 27;
		v = x * 0x2545F4914F6CDD1DULL;
		for (size_t b = 0; b < 8 && i + b < len; b++) {
			out[i + b] = (uint8_t)(v >> (8 * b));
		}
	}
}

/* The directory a path is in, and the last name of it. */
static int
split_path(struct apfs_vol *vol, const char *path, uint64_t *dir, const char **leaf, size_t *len)
{
	char buf[1024];
	const char *slash = strrchr(path, '/');
	uint8_t dt;

	*dir = APFS_ROOT_DIR_INO_NUM;
	if (slash == NULL) {
		*leaf = path;
	} else {
		size_t n = (size_t)(slash - path);

		*leaf = slash + 1;
		if (n > 0) {
			if (n >= sizeof(buf)) {
				return ENAMETOOLONG;
			}
			memcpy(buf, path, n);
			buf[n] = 0;
			int error = apfs_path_lookup(vol, buf, dir, &dt);

			if (error) {
				return error;
			}
		}
	}
	*len = strlen(*leaf);
	return 0;
}

static int
find(struct apfs_vol *vol, const char *path, uint64_t *id)
{
	uint8_t dt;

	return apfs_path_lookup(vol, path, id, &dt);
}

static uint64_t
num(const char *s)
{
	return s != NULL ? strtoull(s, NULL, 0) : 0;
}

static int
one(struct apfs_wtx *w, struct apfs_vol *vol, char **argv, int argc, char *out, size_t outlen)
{
	const char *op = argv[0], *leaf;
	uint64_t dir, id = 0;
	size_t len;
	int error = 0;

	out[0] = 0;
	if (strcmp(op, "commit") == 0) {
		return apfs_wtx_commit(w);
	}
	if (strcmp(op, "reap-orphans") == 0) {
		return apfs_w_reap_orphans(w);
	}
	if (strcmp(op, "reap") == 0 && argc > 1) {
		return apfs_w_reap(w, num(argv[1]));
	}
	if (argc < 2) {
		return EINVAL;
	}
	if (strcmp(op, "mkdir") == 0 || strcmp(op, "create") == 0 || strcmp(op, "symlink") == 0 ||
	    strcmp(op, "mknod") == 0) {
		struct apfs_wnew a = { .uid = 0, .gid = 0 };

		error = split_path(vol, argv[1], &dir, &leaf, &len);
		if (error) {
			return error;
		}
		if (op[0] == 'm' && op[1] == 'k' && op[2] == 'd') {
			a.mode = (uint16_t)(APFS_S_IFDIR | (num(argv[2]) & 07777));
		} else if (op[0] == 'c') {
			a.mode = (uint16_t)(APFS_S_IFREG | (num(argv[2]) & 07777));
		} else if (op[0] == 's') {
			a.mode = APFS_S_IFLNK | 0755;
			a.target = argc > 2 ? argv[2] : "";
			a.target_len = strlen(a.target);
		} else {
			a.mode = (uint16_t)num(argv[2]);
			a.rdev = (uint32_t)num(argc > 3 ? argv[3] : NULL);
		}
		error = apfs_w_create(w, dir, leaf, len, &a, &id);
		snprintf(out, outlen, " %" PRIu64, id);
		return error;
	}
	if (strcmp(op, "link") == 0 && argc > 2) {
		error = find(vol, argv[1], &id);
		if (error == 0) {
			error = split_path(vol, argv[2], &dir, &leaf, &len);
		}
		return error ? error : apfs_w_link(w, id, dir, leaf, len);
	}
	if (strcmp(op, "unlink") == 0 || strcmp(op, "rmdir") == 0 || strcmp(op, "orphan") == 0) {
		bool gone = false;

		error = split_path(vol, argv[1], &dir, &leaf, &len);
		if (error == 0) {
			error = apfs_w_remove(w, dir, leaf, len, op[0] == 'r', op[0] == 'o', &id, &gone);
		}
		snprintf(out, outlen, " %" PRIu64 " %s", id, gone ? "gone" : "kept");
		return error;
	}
	if (strcmp(op, "rename") == 0 && argc > 2) {
		const char *tleaf;
		uint64_t tdir, replaced = 0;
		size_t tlen;
		bool gone = false;
		uint32_t flags = argc > 3 ? (strcmp(argv[3], "swap") == 0 ? APFS_WREN_SWAP :
		    strcmp(argv[3], "excl") == 0 ? APFS_WREN_EXCL : 0) : 0;

		error = split_path(vol, argv[1], &dir, &leaf, &len);
		if (error == 0) {
			error = split_path(vol, argv[2], &tdir, &tleaf, &tlen);
		}
		if (error == 0) {
			error = apfs_w_rename(w, dir, leaf, len, tdir, tleaf, tlen, flags, false, &replaced, &gone);
		}
		snprintf(out, outlen, " %" PRIu64, replaced);
		return error;
	}
	if (strcmp(op, "clone") == 0 && argc > 2) {
		struct apfs_wattr top = { 0 };
		uint64_t made = 0;

		error = find(vol, argv[1], &id);
		if (error == 0) {
			error = split_path(vol, argv[2], &dir, &leaf, &len);
		}
		if (error == 0) {
			error = apfs_w_clone(w, id, dir, leaf, len, &top, 0, &made);
		}
		snprintf(out, outlen, " %" PRIu64, made);
		return error;
	}
	error = find(vol, argv[1], &id);
	if (error) {
		return error;
	}
	if (strcmp(op, "truncate") == 0 && argc > 2) {
		return apfs_w_truncate(w, id, num(argv[2]));
	}
	if (strcmp(op, "write") == 0 && argc > 4) {
		size_t n = (size_t)num(argv[4]);
		uint8_t *data = malloc(n ? n : 1);

		if (data == NULL) {
			return ENOMEM;
		}
		gen_bytes(num(argv[3]), data, n);
		error = apfs_w_write(w, id, num(argv[2]), data, n);
		free(data);
		return error;
	}
	if (strcmp(op, "setxattr") == 0 && argc > 4) {
		size_t n = (size_t)num(argv[4]);
		uint8_t *data = malloc(n ? n : 1);

		if (data == NULL) {
			return ENOMEM;
		}
		gen_bytes(num(argv[3]), data, n);
		error = apfs_w_setxattr(w, id, argv[2], data, n, 0);
		free(data);
		return error;
	}
	if (strcmp(op, "rmxattr") == 0 && argc > 2) {
		return apfs_w_rmxattr(w, id, argv[2]);
	}
	if (strcmp(op, "docid") == 0) {
		uint32_t doc = 0;

		error = apfs_w_docid(w, id, &doc);
		snprintf(out, outlen, " %u", doc);
		return error;
	}
	struct apfs_wattr a = { 0 };

	if (strcmp(op, "chmod") == 0 && argc > 2) {
		a.set = APFS_WA_MODE;
		a.mode = (uint16_t)num(argv[2]);
	} else if (strcmp(op, "chown") == 0 && argc > 3) {
		a.set = APFS_WA_UID | APFS_WA_GID;
		a.uid = (uint32_t)num(argv[2]);
		a.gid = (uint32_t)num(argv[3]);
	} else if (strcmp(op, "chflags") == 0 && argc > 2) {
		a.set = APFS_WA_FLAGS;
		a.flags = (uint32_t)num(argv[2]);
	} else if (strcmp(op, "utimes") == 0 && argc > 3) {
		a.set = APFS_WA_ATIME | APFS_WA_MTIME;
		a.atime = num(argv[2]);
		a.mtime = num(argv[3]);
	} else {
		return EINVAL;
	}
	return apfs_w_setattr(w, id, &a);
}

int
cmd_do(const char *path, const char *script)
{
	struct apfs_nx nx;
	struct apfs_vol vol;
	struct apfs_wtx *w = NULL;
	FILE *f = script != NULL && strcmp(script, "-") != 0 ? fopen(script, "r") : stdin;
	char line[4096], out[128];
	int error, failed = 0, lineno = 0;

	if (f == NULL) {
		perror(script);
		return 1;
	}
	want_write = true;
	if (open_image(path, &nx, &vol) != 0) {
		return 1;
	}
	error = apfs_wtx_attach(&vol, &w);
	if (error) {
		fprintf(stderr, "apfs-tool: %s: cannot write: %s\n", path, strerror(error));
		apfs_nx_close(&nx);
		return 1;
	}
	while (fgets(line, sizeof(line), f) != NULL) {
		char *argv[8], *p = line;
		int argc = 0;

		lineno++;
		while (argc < 8) {
			while (*p == ' ' || *p == '\t' || *p == '\n') {
				p++;
			}
			if (*p == 0 || *p == '#') {
				break;
			}
			argv[argc++] = p;
			while (*p != 0 && *p != ' ' && *p != '\t' && *p != '\n') {
				p++;
			}
			if (*p != 0) {
				*p++ = 0;
			}
		}
		if (argc == 0) {
			continue;
		}
		error = one(w, &vol, argv, argc, out, sizeof(out));
		printf("%d %s %s%s\n", lineno, argv[0], error ? strerror(error) : "ok", error ? "" : out);
		if (error == EIO || apfs_wtx_broken(w)) {
			failed = 1;
			break;
		}
	}
	if (!failed) {
		error = apfs_wtx_commit(w);
		printf("end commit %s\n", error ? strerror(error) : "ok");
		failed = error != 0;
	}
	uint64_t commits, blocks, ticks;

	apfs_wtx_stats(w, &commits, &blocks, &ticks);
	printf("commits %" PRIu64 " blocks %" PRIu64 " ms %.1f\n", commits, blocks, ticks / 1e6);
	apfs_wtx_detach(w);
	apfs_nx_close(&nx);
	if (f != stdin) {
		fclose(f);
	}
	return failed;
}
