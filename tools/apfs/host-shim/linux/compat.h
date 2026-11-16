/* What the kernel's APFS code takes from BSD headers that glibc does not have (build-tool.sh off macOS). */
#ifndef APFS_HOST_SHIM_LINUX_COMPAT_H
#define APFS_HOST_SHIM_LINUX_COMPAT_H
#include <errno.h>

#ifndef ENOATTR
#define ENOATTR ENODATA
#endif
#ifndef __unused
#define __unused __attribute__((unused))
#endif
#endif
