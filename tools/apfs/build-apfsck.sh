#!/bin/bash
# Build apfsck, the APFS checker of linux-apfs/apfsprogs (GPL-2.0, not in this repository), for
# the checks in tools/apfs: cloned into xnu-deps/build/apfsprogs at the commit below, with
# apfsprogs-host.patch on top (the reasons are at the head of the patch).  Built again when the
# patch is newer than apfsck.  Prints apfsck's path.
#
# usage: tools/apfs/build-apfsck.sh
set -eu
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
APFSPROGS=$ROOT/xnu-deps/build/apfsprogs
APFSPROGS_URL=https://github.com/linux-apfs/apfsprogs.git
APFSPROGS_COMMIT=1bd1c1c2140d13d1b6d2e03379316572bee70aa3   # "mkapfs: set INODE_NO_RSRC_FORK on the directories"
PATCH=$HERE/apfsprogs-host.patch
APFSCK=$APFSPROGS/apfsck/apfsck

if [ ! -x "$APFSCK" ] || [ "$PATCH" -nt "$APFSCK" ]; then
	if [ ! -d "$APFSPROGS/.git" ]; then
		mkdir -p "$(dirname "$APFSPROGS")"
		git clone -q "$APFSPROGS_URL" "$APFSPROGS"
	fi
	git -C "$APFSPROGS" checkout -q -f "$APFSPROGS_COMMIT"
	git -C "$APFSPROGS" clean -q -f -d include apfsck
	git -C "$APFSPROGS" apply "$PATCH"
	make -C "$APFSPROGS/apfsck" --silent --no-print-directory clean > /dev/null
	make -C "$APFSPROGS/apfsck" --silent --no-print-directory > /dev/null
fi
echo "$APFSCK"
