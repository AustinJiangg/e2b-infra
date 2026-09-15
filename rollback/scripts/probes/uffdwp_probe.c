// 测这台 arm64 内核到底支不支持 uffd 写保护，以及 pagemap 第 57 位会不会被置上。
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <linux/userfaultfd.h>

#define PS 4096

static int pagemap_bits(void *addr, unsigned long long *ent) {
    int fd = open("/proc/self/pagemap", O_RDONLY);
    if (fd < 0) return -1;
    off_t off = ((unsigned long)addr / PS) * 8;
    int r = pread(fd, ent, 8, off) == 8 ? 0 : -1;
    close(fd);
    return r;
}

int main(void) {
    int uffd = syscall(SYS_userfaultfd, O_CLOEXEC | O_NONBLOCK);
    if (uffd < 0) { perror("userfaultfd"); return 1; }

    struct uffdio_api api = {0};
    api.api = UFFD_API;
    api.features = 0;
    if (ioctl(uffd, UFFDIO_API, &api)) { perror("UFFDIO_API(探测)"); return 1; }
    printf("内核支持的 features 位图 = 0x%llx\n", (unsigned long long)api.features);
#ifdef UFFD_FEATURE_PAGEFAULT_FLAG_WP
    printf("  PAGEFAULT_FLAG_WP (0x%llx) : %s\n",
           (unsigned long long)UFFD_FEATURE_PAGEFAULT_FLAG_WP,
           (api.features & UFFD_FEATURE_PAGEFAULT_FLAG_WP) ? "支持" : "**不支持**");
#endif
#ifdef UFFD_FEATURE_WP_ASYNC
    printf("  WP_ASYNC          (0x%llx) : %s\n",
           (unsigned long long)UFFD_FEATURE_WP_ASYNC,
           (api.features & UFFD_FEATURE_WP_ASYNC) ? "支持" : "不支持");
#endif

    void *m = mmap(NULL, PS * 4, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (m == MAP_FAILED) { perror("mmap"); return 1; }

    struct uffdio_register reg = {0};
    reg.range.start = (unsigned long)m;
    reg.range.len = PS * 4;
    reg.mode = UFFDIO_REGISTER_MODE_MISSING;
    if (ioctl(uffd, UFFDIO_REGISTER, &reg))
        printf("注册 MISSING            : 失败 %s\n", strerror(errno));
    else
        printf("注册 MISSING            : 成功\n");

    // 再开一个 fd 测 MISSING|WP
    int uffd2 = syscall(SYS_userfaultfd, O_CLOEXEC | O_NONBLOCK);
    struct uffdio_api api2 = {0};
    api2.api = UFFD_API;
#ifdef UFFD_FEATURE_PAGEFAULT_FLAG_WP
    api2.features = UFFD_FEATURE_PAGEFAULT_FLAG_WP;
#endif
    if (ioctl(uffd2, UFFDIO_API, &api2))
        printf("协商 PAGEFAULT_FLAG_WP  : 失败 %s\n", strerror(errno));
    else
        printf("协商 PAGEFAULT_FLAG_WP  : 成功\n");

    void *m2 = mmap(NULL, PS * 4, PROT_READ | PROT_WRITE,
                    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    struct uffdio_register reg2 = {0};
    reg2.range.start = (unsigned long)m2;
    reg2.range.len = PS * 4;
    reg2.mode = UFFDIO_REGISTER_MODE_MISSING | UFFDIO_REGISTER_MODE_WP;
    if (ioctl(uffd2, UFFDIO_REGISTER, &reg2)) {
        printf("注册 MISSING|WP         : **失败** %s  → 这台内核用不了 uffd 写保护\n", strerror(errno));
        return 0;
    }
    printf("注册 MISSING|WP         : 成功\n");

    // 用 UFFDIO_COPY + MODE_WP 填一页，然后看 pagemap 第 57 位
    char *src = malloc(PS);
    memset(src, 0xAB, PS);
    struct uffdio_copy cp = {0};
    cp.dst = (unsigned long)m2;
    cp.src = (unsigned long)src;
    cp.len = PS;
    cp.mode = UFFDIO_COPY_MODE_WP;
    if (ioctl(uffd2, UFFDIO_COPY, &cp)) {
        printf("UFFDIO_COPY + MODE_WP   : **失败** %s\n", strerror(errno));
        return 0;
    }
    printf("UFFDIO_COPY + MODE_WP   : 成功，拷了 %lld 字节\n", (long long)cp.copy);

    unsigned long long ent = 0;
    pagemap_bits(m2, &ent);
    printf("pagemap 项 = 0x%llx   present(63)=%d  uffd_wp(57)=%d\n",
           ent, (int)((ent >> 63) & 1), (int)((ent >> 57) & 1));
    printf("\n结论：%s\n", ((ent >> 57) & 1)
        ? "这台内核支持 uffd 写保护，且 pagemap 第 57 位能正确反映它 —— FC 那套判据在此可用。"
        : "写保护装上了但 pagemap 第 57 位没置 —— FC 的判脏方式在此内核上失效。");

    // 对照：不带 MODE_WP 填第二页
    cp.dst = (unsigned long)m2 + PS;
    cp.mode = 0;
    if (!ioctl(uffd2, UFFDIO_COPY, &cp)) {
        pagemap_bits((char *)m2 + PS, &ent);
        printf("对照（不带 MODE_WP 填的一页）: present=%d uffd_wp=%d  ← 就是 ARM 补丁注释掉之后的样子\n",
               (int)((ent >> 63) & 1), (int)((ent >> 57) & 1));
    }
    return 0;
}
