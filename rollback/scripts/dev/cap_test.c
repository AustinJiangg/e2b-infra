/* KVM_CAP_ARM_HW_DIRTY_STATE_TRACK (502) probe: reports whether this host can
 * do HDBSS hardware dirty tracking, without needing Firecracker or a sandbox.
 *
 * Checks two things, because they can disagree:
 *   1. KVM_CHECK_EXTENSION on /dev/kvm — the kernel knows the capability
 *   2. KVM_ENABLE_CAP on a throwaway VM fd — it actually turns on
 *
 * Build: gcc -static -o cap_test cap_test.c
 */
#include <errno.h>
#include <fcntl.h>
#include <linux/kvm.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#ifndef KVM_CAP_ARM_HW_DIRTY_STATE_TRACK
#define KVM_CAP_ARM_HW_DIRTY_STATE_TRACK 502
#endif

int main(void)
{
	int kvm = open("/dev/kvm", O_RDWR | O_CLOEXEC);
	if (kvm < 0) {
		printf("cap 502: unknown (cannot open /dev/kvm: %s)\n", strerror(errno));
		return 2;
	}

	int ext = ioctl(kvm, KVM_CHECK_EXTENSION, KVM_CAP_ARM_HW_DIRTY_STATE_TRACK);
	printf("KVM_CHECK_EXTENSION(502) = %d\n", ext);

	int vm = ioctl(kvm, KVM_CREATE_VM, 0);
	if (vm < 0) {
		printf("cap 502: unknown (cannot create a test VM: %s)\n", strerror(errno));
		close(kvm);
		return 2;
	}

	struct kvm_enable_cap cap;
	memset(&cap, 0, sizeof(cap));
	cap.cap = KVM_CAP_ARM_HW_DIRTY_STATE_TRACK;
	cap.args[0] = 1; /* buffer order 1 = 8 KiB per vCPU, same default Firecracker uses */

	int ret = ioctl(vm, KVM_ENABLE_CAP, &cap);
	if (ret < 0) {
		printf("KVM_ENABLE_CAP(502, order 1) = %d (%s)\n", ret, strerror(errno));
		printf("cap 502: NOT supported -> Firecracker will fall back to KVM write-protect\n");
		close(vm);
		close(kvm);
		return 1;
	}

	printf("KVM_ENABLE_CAP(502, order 1) = %d\n", ret);
	printf("cap 502: supported -> Firecracker will use HDBSS\n");
	close(vm);
	close(kvm);
	return 0;
}
