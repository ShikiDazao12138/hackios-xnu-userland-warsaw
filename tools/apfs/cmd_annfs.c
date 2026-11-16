/*
 * apfs-tool annfs IMAGE HEAD [TAIL]: the /private/var kept in ANNFS001 (the format of
 * bsd/miscfs/ramfs/ramfs_disk.inc on darwin_var) as an APFS volume over the same data blocks.
 *
 * IMAGE is a container with one empty volume that mkapfs.py made for the partition less its
 * last 32 MiB (tools/apfs/annfs-to-apfs.py); HEAD holds at least the partition's first 64 MiB,
 * the two metadata slots, and TAIL what the partition has past the container's end.  The
 * newer valid slot is read and every object reachable from the
 * root is made again with its inode number (those below 16 are APFS's own and get new ones),
 * mode, owner, BSD flags, times, protection class, document id, sync-root mark and extended
 * attributes; a name that appears twice for one inode becomes a hard link.  A regular file is
 * given the blocks its data is in: ANNFS page n (16 KiB at 512 MiB + 16 KiB * n) is APFS
 * blocks 131072 + 4n to 131072 + 4n + 3, the ones within the file's length.  ANNFS allocates
 * next-fit across the whole partition, so some of them may be past the container: those are
 * copied from TAIL into new blocks.  Everything the conversion allocates or writes is below
 * block 131072, so IMAGE's first 512 MiB is all of it that goes to the partition.
 *
 * Not carried over: device numbers (ANNFS keeps none) and objects no directory names.
 */

#include <zlib.h>

#include "apfs_tool.h"

#define ANNFS_SLOT1       (32u << 20)
#define ANNFS_SLOT_MAX    (32u << 20)
#define ANNFS_SUPER       4096u
#define ANNFS_PAGE        16384u
#define ANNFS_DATA_BLOCK  131072u           /* 512 MiB in blocks of 4 KiB */
#define ANNFS_ROOT        2u
#define ANNFS_SYNCROOT    0x1u              /* RAMFS_IF_SYNCROOT */
#define A8(n)             (((uint64_t)(n) + 7) & ~7ULL)

/* the records as ramfs_disk.inc lays them out (version 3) */
struct annfs_super {
	char     magic[8];
	uint32_t version, page_size;
	uint64_t generation, payload_bytes, next_ino;
	uint32_t node_count, payload_crc, super_crc;
};

struct annfs_inode {
	uint64_t ino, size, parent_ino;
	uint32_t type, mode, uid, gid, bsdflags, pclass, nlink, target_len, nblocks, nxattrs, ndirents;
	uint32_t iflags, document_id, write_gen;
	int64_t  times[8];                  /* atime, mtime, ctime, btime: seconds, nanoseconds */
};
_Static_assert(sizeof(struct annfs_inode) == 144, "ramfs_disk.inc: struct annfs_inode_rec");

struct node {
	const struct annfs_inode *rec;
	const char *target;
	const uint32_t *blocks;
	const uint8_t *xattrs, *dirents;
	uint64_t made;                      /* its APFS inode, once made */
};

struct conv {
	struct apfs_wtx *w;
	struct apfs_nx *nx;
	struct node *nodes;
	uint32_t n;
	const uint8_t *tail;                /* the partition from the container's end on */
	uint64_t tail_blocks;
	uint64_t links, adopted, moved, xattrs, skipped_types;
};

static int
by_ino(const void *a, const void *b)
{
	uint64_t x = ((const struct node *)a)->rec->ino, y = ((const struct node *)b)->rec->ino;

	return x < y ? -1 : x > y;
}

static struct node *
find(struct conv *c, uint64_t ino)
{
	uint32_t lo = 0, hi = c->n;

	while (lo < hi) {
		uint32_t mid = (lo + hi) / 2;

		if (c->nodes[mid].rec->ino == ino) {
			return &c->nodes[mid];
		}
		if (c->nodes[mid].rec->ino < ino) {
			lo = mid + 1;
		} else {
			hi = mid;
		}
	}
	return NULL;
}

/* The newer of the two slots that is whole, as ramfs_disk_super_valid() accepts it. */
static const struct annfs_super *
pick_slot(const uint8_t *head, size_t len)
{
	const struct annfs_super *best = NULL;

	for (size_t off = 0; off <= ANNFS_SLOT1 && off + ANNFS_SUPER <= len; off += ANNFS_SLOT1) {
		const struct annfs_super *s = (const void *)(head + off);

		if (memcmp(s->magic, "ANNFS001", 8) != 0 || s->version != 3 || s->page_size != ANNFS_PAGE ||
		    s->generation == 0 || s->node_count == 0 || s->payload_bytes > ANNFS_SLOT_MAX - ANNFS_SUPER ||
		    off + ANNFS_SUPER + s->payload_bytes > len ||
		    crc32(0, (const void *)s, offsetof(struct annfs_super, super_crc)) != s->super_crc ||
		    crc32(0, head + off + ANNFS_SUPER, (uInt)s->payload_bytes) != s->payload_crc) {
			continue;
		}
		if (best == NULL || s->generation > best->generation) {
			best = s;
		}
	}
	return best;
}

static int
parse(struct conv *c, const struct annfs_super *s)
{
	const uint8_t *base = (const uint8_t *)s + ANNFS_SUPER;
	uint64_t at = 0, end = s->payload_bytes;

	c->nodes = calloc(s->node_count, sizeof(*c->nodes));
	if (c->nodes == NULL) {
		return ENOMEM;
	}
	for (c->n = 0; c->n < s->node_count; c->n++) {
		struct node *nd = &c->nodes[c->n];
		const struct annfs_inode *r = (const void *)(base + at);

		if (at + sizeof(*r) > end) {
			return EINVAL;
		}
		nd->rec = r;
		at += sizeof(*r);
		nd->target = (const char *)(base + at);
		at += A8(r->target_len);
		nd->blocks = (const uint32_t *)(base + at);
		at += A8((uint64_t)r->nblocks * 4);
		nd->xattrs = base + at;
		for (uint32_t x = 0; x < r->nxattrs && at + 8 <= end; x++) {
			const uint32_t *h = (const void *)(base + at);

			at += 8 + A8((uint64_t)h[0] + h[1]);
		}
		nd->dirents = base + at;
		for (uint32_t d = 0; d < r->ndirents && at + 16 <= end; d++) {
			at += 16 + A8(*(const uint32_t *)(base + at + 8));
		}
		if (at > end) {
			return EINVAL;
		}
	}
	if (at != end) {
		return EINVAL;
	}
	qsort(c->nodes, c->n, sizeof(*c->nodes), by_ino);
	for (uint32_t i = 1; i < c->n; i++) {
		if (c->nodes[i].rec->ino == c->nodes[i - 1].rec->ino) {
			return EINVAL;
		}
	}
	return 0;
}

static uint16_t
type_bits(uint32_t vtype)
{
	/* enum vtype: VREG 1, VDIR 2, VBLK 3, VCHR 4, VLNK 5, VSOCK 6, VFIFO 7 */
	static const uint16_t bits[] = { 0, APFS_S_IFREG, APFS_S_IFDIR, APFS_S_IFBLK, APFS_S_IFCHR,
		                         APFS_S_IFLNK, APFS_S_IFSOCK, APFS_S_IFIFO };

	return vtype < sizeof(bits) / sizeof(bits[0]) ? bits[vtype] : 0;
}

static uint64_t
ns(const int64_t *t)
{
	return t[0] < 0 ? 0 : (uint64_t)t[0] * 1000000000ULL + (uint64_t)t[1];
}

/* Blocks of a file past the container: into new ones below block 131072, copied from TAIL. */
static int
move_data(struct conv *c, const struct node *nd, uint64_t off, uint64_t phys, uint64_t blocks)
{
	uint64_t from = phys - c->nx->block_count;
	int error = from + blocks > c->tail_blocks ? EFBIG : 0;

	while (error == 0 && blocks > 0) {
		uint64_t p = 0, run = 0, n;

		error = apfs_w_map(c->w, nd->made, off, blocks * 4096, true, &p, &run);
		if (error == 0 && p == 0) {
			error = EIO;
		}
		if (error == 0) {
			n = run / 4096 < blocks ? run / 4096 : blocks;
			error = c->nx->dev.write(c->nx->dev.ctx, p * 4096, c->tail + from * 4096, (size_t)n * 4096);
			off += n * 4096;
			from += n;
			blocks -= n;
			c->moved += n;
		}
	}
	return error;
}

/* A regular file's length, then its pages where they are, run by run. */
static int
give_data(struct conv *c, const struct node *nd)
{
	const struct annfs_inode *r = nd->rec;
	uint64_t blocks = (r->size + 4095) / 4096, off = 0, phys = 0, run = 0, end = c->nx->block_count;
	int error = apfs_w_truncate(c->w, nd->made, r->size);

	for (uint64_t b = 0; error == 0 && b <= blocks; b++) {
		uint32_t page = b < blocks && b / 4 < r->nblocks ? nd->blocks[b / 4] : 0;
		uint64_t p = page != 0 ? ANNFS_DATA_BLOCK + (uint64_t)page * 4 + b % 4 : 0;

		if (run > 0 && p == phys + run && (p < end) == (phys < end)) {
			run++;
			continue;
		}
		if (run > 0 && phys < end) {
			error = apfs_w_adopt(c->w, nd->made, off, phys, run);
			c->adopted += run;
		} else if (run > 0) {
			error = move_data(c, nd, off, phys, run);
		}
		off = b * 4096;
		phys = p;
		run = p != 0;
	}
	return error;
}

static int
give_xattrs(struct conv *c, const struct node *nd)
{
	const uint8_t *p = nd->xattrs;
	int error = 0;

	for (uint32_t x = 0; error == 0 && x < nd->rec->nxattrs; x++) {
		uint32_t nl = ((const uint32_t *)p)[0], size = ((const uint32_t *)p)[1];
		char name[256];

		if (nl == 0 || nl >= sizeof(name)) {
			return EINVAL;
		}
		memcpy(name, p + 8, nl);
		name[nl] = 0;
		error = apfs_w_setxattr(c->w, nd->made, name, p + 8 + nl, size, 0);
		if (error) {
			fprintf(stderr, "apfs-tool annfs: inode %" PRIu64 ": attribute %s: %s\n", nd->rec->ino, name,
			    strerror(error));
		}
		c->xattrs++;
		p += 8 + A8((uint64_t)nl + size);
	}
	return error;
}

/* Every entry of a directory already made: the objects it names, made or linked. */
static int
fill(struct conv *c, const struct node *dir, struct node **queue, uint32_t *tail)
{
	const uint8_t *p = dir->dirents;
	int error = 0;

	for (uint32_t d = 0; error == 0 && d < dir->rec->ndirents; d++) {
		uint64_t child_ino = *(const uint64_t *)p;
		uint32_t nl = *(const uint32_t *)(p + 8);
		const char *name = (const char *)(p + 16);
		struct node *ch = find(c, child_ino);

		p += 16 + A8(nl);
		if (ch == NULL) {
			error = EINVAL;
		} else if (ch->made != 0) {
			error = type_bits(ch->rec->type) == APFS_S_IFDIR ? EINVAL :
			    apfs_w_link(c->w, ch->made, dir->made, name, nl);
			c->links++;
		} else {
			const struct annfs_inode *r = ch->rec;
			struct apfs_wnew a = {
				.mode = (uint16_t)(type_bits(r->type) | (r->mode & 07777)), .uid = r->uid, .gid = r->gid,
				.pclass = r->pclass, .target = ch->target, .target_len = r->target_len,
				.id = r->ino >= APFS_MIN_USER_INO_NUM ? r->ino : 0,
			};

			if (type_bits(r->type) == 0 || type_bits(r->type) == APFS_S_IFSOCK) {
				c->skipped_types++;
				continue;
			}
			error = apfs_w_create(c->w, dir->made, name, nl, &a, &ch->made);
			if (error == 0 && r->type == 1) {
				error = give_data(c, ch);
			}
			if (error == 0 && r->type == 2) {
				queue[(*tail)++] = ch;
			}
		}
		if (error) {
			fprintf(stderr, "apfs-tool annfs: \"%.*s\" in directory inode %" PRIu64 ": %s\n", (int)nl, name,
			    dir->rec->ino, strerror(error));
		}
		if (error == 0 && apfs_wtx_dirty_blocks(c->w) > 4096) {
			error = apfs_wtx_commit(c->w);
		}
	}
	return error;
}

/* What only settles once every name is in: attributes, document ids, and times last. */
static int
finish(struct conv *c, struct node *nd)
{
	const struct annfs_inode *r = nd->rec;
	struct apfs_wattr a = {
		.set = APFS_WA_MODE | APFS_WA_UID | APFS_WA_GID | APFS_WA_FLAGS | APFS_WA_ATIME | APFS_WA_MTIME |
		    APFS_WA_CTIME | APFS_WA_CRTIME,
		.mode = (uint16_t)(r->mode & 07777), .uid = r->uid, .gid = r->gid, .flags = r->bsdflags,
		.atime = ns(&r->times[0]), .mtime = ns(&r->times[2]), .ctime = ns(&r->times[4]), .crtime = ns(&r->times[6]),
	};
	int error = give_xattrs(c, nd);

	if (nd->made == APFS_ROOT_DIR_INO_NUM) {
		a.set |= APFS_WA_PCLASS;        /* (the others had theirs from apfs_w_create) */
		a.pclass = r->pclass;
	}
	if (r->iflags & ANNFS_SYNCROOT) {
		a.set |= APFS_WA_IFLAGS;
		a.iflags_on = APFS_INODE_IS_SYNC_ROOT;
	}
	if (error == 0 && r->document_id != 0) {
		error = apfs_w_set_docid(c->w, nd->made, r->document_id);
	}
	if (error == 0) {
		error = apfs_w_setattr(c->w, nd->made, &a);
	}
	if (error == 0 && apfs_wtx_dirty_blocks(c->w) > 4096) {
		error = apfs_wtx_commit(c->w);
	}
	return error;
}

/* The whole of a file, up to cap bytes, into memory. */
static uint8_t *
slurp(const char *path, size_t cap, size_t *len)
{
	uint8_t *buf = malloc(cap ? cap : 1);
	int fd = open(path, O_RDONLY);
	ssize_t got = 1;

	*len = 0;
	while (buf != NULL && fd >= 0 && *len < cap && (got = read(fd, buf + *len, cap - *len)) > 0) {
		*len += (size_t)got;
	}
	if (fd < 0 || got < 0 || buf == NULL) {
		perror(path);
		free(buf);
		buf = NULL;
	}
	if (fd >= 0) {
		close(fd);
	}
	return buf;
}

int
cmd_annfs(const char *path, const char *head_path, const char *tail_path)
{
	struct apfs_nx nx;
	struct apfs_vol vol;
	struct conv c = { .nx = &nx };
	struct node **queue = NULL, *root;
	uint8_t *head, *tail = NULL;
	const struct annfs_super *s;
	uint64_t max_ino = 0, files = 0;
	uint32_t qh = 0, qt = 0;
	size_t len, tail_len = 0;
	int error = 0;

	head = slurp(head_path, 2 * ANNFS_SLOT_MAX, &len);
	if (head == NULL || (tail_path != NULL && (tail = slurp(tail_path, 64u << 20, &tail_len)) == NULL)) {
		return 1;
	}
	s = pick_slot(head, len);
	if (s == NULL || parse(&c, s) != 0 || (root = find(&c, ANNFS_ROOT)) == NULL || root->rec->type != 2) {
		fprintf(stderr, "apfs-tool annfs: %s: no whole ANNFS001 metadata slot\n", head_path);
		return 1;
	}
	printf("annfs: generation %" PRIu64 ", %u objects\n", s->generation, c.n);
	want_write = true;
	if (open_image(path, &nx, &vol) != 0) {
		return 1;
	}
	c.tail = tail;
	c.tail_blocks = tail_len / 4096;
	error = apfs_wtx_attach(&vol, &c.w);
	if (error) {
		fprintf(stderr, "apfs-tool annfs: %s: cannot write: %s\n", path, strerror(error));
		apfs_nx_close(&nx);
		return 1;
	}
	for (uint32_t i = 0; i < c.n; i++) {
		const struct annfs_inode *r = c.nodes[i].rec;

		max_ino = r->ino > max_ino ? r->ino : max_ino;
		for (uint32_t b = 0; r->type == 1 && b < r->nblocks; b++) {
			uint64_t last = ANNFS_DATA_BLOCK + (uint64_t)c.nodes[i].blocks[b] * 4 + 3;

			if (c.nodes[i].blocks[b] != 0 && last >= nx.block_count + c.tail_blocks) {
				fprintf(stderr, "apfs-tool annfs: inode %" PRIu64 " has data at block %" PRIu64
				    ", past the container and the %" PRIu64 " blocks of %s\n", r->ino, last, c.tail_blocks,
				    tail_path != NULL ? tail_path : "TAIL");
				error = EFBIG;
			}
		}
	}
	queue = calloc(c.n, sizeof(*queue));
	error = error ? error : queue == NULL ? ENOMEM : 0;
	if (error == 0) {
		apfs_w_ids_from(c.w, max_ino + 1);
		apfs_w_alloc_below(c.w, ANNFS_DATA_BLOCK);
		root->made = APFS_ROOT_DIR_INO_NUM;
		queue[qt++] = root;
	}
	while (error == 0 && qh < qt) {
		error = fill(&c, queue[qh++], queue, &qt);
	}
	for (uint32_t i = 0; error == 0 && i < c.n; i++) {
		if (c.nodes[i].made != 0) {
			error = finish(&c, &c.nodes[i]);
			files++;
		}
	}
	if (error == 0) {
		error = apfs_wtx_commit(c.w);
	}
	if (error == 0) {
		printf("annfs: %" PRIu64 " objects made, %" PRIu64 " more names (hard links), %" PRIu64
		    " extended attributes, %" PRIu64 " blocks of data adopted, %" PRIu64 " copied from past the container; %"
		    PRIu64 " objects left out (%" PRIu64 " names of sockets or unknown types, the rest named by no directory)\n",
		    files, c.links, c.xattrs, c.adopted, c.moved, (uint64_t)c.n - files, c.skipped_types);
	} else {
		fprintf(stderr, "apfs-tool annfs: %s: stopped: %s; nothing of the image is to be used\n", path,
		    strerror(error));
	}
	apfs_wtx_detach(c.w);
	apfs_nx_close(&nx);
	free(queue);
	free(c.nodes);
	free(head);
	free(tail);
	return error != 0;
}
