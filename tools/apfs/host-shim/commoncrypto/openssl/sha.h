/*
 * The one OpenSSL call apfs_lib.c makes outside the kernel, SHA256(), from CommonCrypto in
 * libSystem: on macOS apfs-tool then needs no OpenSSL and runs on any macOS it was built for.
 */
#ifndef APFS_HOST_SHIM_SHA_H
#define APFS_HOST_SHIM_SHA_H
#include <CommonCrypto/CommonDigest.h>

#define SHA256(data, len, out) CC_SHA256((data), (CC_LONG)(len), (out))
#endif
