%define debug_package %{nil}
%global tag 2026.09

Name:           e2b-infra
Version:        %{tag}
Release:        4
Summary:        E2B Infra – sandbox runtime (ARM-adapted)
License:        MIT
URL:            https://github.com/e2b-dev/infra
Source0:        https://github.com/e2b-dev/infra/archive/refs/tags/%{tag}.tar.gz#/%{name}-%{tag}.tar.gz
Source1:        busybox_1.35_arm64
Source2:        goose_3.24.2_linux_arm64
Source3:        goose_3.24.2_linux_x86
Source4:        vmlinux.bin.arm
Source5:        vmlinux.bin.x86
Source6:        vmlinux.bin.arm.openeuler
Source7:        patch_e2b.py
Source8:        firecracker.arm
Source9:        e2b-deploy.tar.gz
Patch1:         0001-adapted-for-arm-architecture.patch
Patch2:         0002-fc-launch-dedicated-helper.patch

BuildRequires:  make
BuildRequires:  gcc

%description
E2B Infra self-hosted sandbox runtime.

%prep
%autosetup -p1 -n e2b-infra-%{tag}
cp %{SOURCE1} packages/orchestrator/internal/template/build/core/systeminit/
mkdir -p %{_builddir}/go-toolchain
%ifarch aarch64
    tar -xf %{_sourcedir}/tools-arm64.tar.gz -C %{_builddir}/go-toolchain
%endif
%ifarch x86_64
    tar -xf %{_sourcedir}/tools-amd64.tar.gz -C %{_builddir}/go-toolchain
%endif

tar -xf %{SOURCE9} -C %{_builddir}

%build
export GOROOT=%{_builddir}/go-toolchain/go
export PATH=$GOROOT/bin:$PATH
export GOFLAGS=-mod=vendor
rm -f go.work go.work.sum

pushd packages/db/scripts/seed/postgres
CGO_ENABLED=0 go build -o seed-db seed-db.go
popd

for d in packages/api packages/client-proxy \
         packages/envd packages/db \
         packages/orchestrator; do
    pushd $d
    make build
    popd
done

%install
# 1. 创建主目录
install -d %{buildroot}/opt/e2b-infra/bin
install -d %{buildroot}/opt/e2b-infra

install -d %{buildroot}/opt/e2b-infra/nomad
for hcl in iac/provider-gcp/nomad/jobs/*.hcl; do
    [ -f "$hcl" ] || continue
    install -D -m 644 "$hcl" %{buildroot}/opt/e2b-infra/nomad/$(basename "$hcl")
done

# 2. 安装所有模块编译产物（packages/*/bin/* → /opt/e2b-infra/bin/）
install -D -m 755 packages/db/scripts/seed/postgres/seed-db %{buildroot}/opt/e2b-infra/bin/seed-db
for exe in packages/*/bin/*; do
    [ -f "$exe" ] || continue
    install -D -m 755 "$exe" %{buildroot}/opt/e2b-infra/bin/$(basename "$exe")
done

# 3. 安装各模块 Dockerfile（若存在）
for d in packages/api packages/client-proxy packages/envd packages/db packages/orchestrator; do
    [ -f "$d/Dockerfile" ] || continue
    case "$d" in
        packages/db)
            install -D -m 644 "$d/Dockerfile" %{buildroot}/opt/e2b-infra/bin/db-migrator.Dockerfile
            ;;
        *)
            install -D -m 644 "$d/Dockerfile" %{buildroot}/opt/e2b-infra/bin/$(basename "$d").Dockerfile
            ;;
    esac
done

# 4. 扁平安装所有运维脚本 & 配置（直接放到 /opt/e2b-infra/）
install -D -m 755 ./iac/provider-gcp/nomad-cluster-disk-image/setup/install-consul.sh %{buildroot}/opt/e2b-infra/install-consul.sh
install -D -m 755 ./iac/provider-gcp/nomad-cluster-disk-image/setup/install-nomad.sh %{buildroot}/opt/e2b-infra/install-nomad.sh
install -D -m 644 ./iac/provider-gcp/nomad-cluster-disk-image/setup/nomad.service %{buildroot}/opt/e2b-infra/nomad.service
install -D -m 755 ./iac/provider-gcp/nomad-cluster/scripts/uninstall-consul.sh %{buildroot}/opt/e2b-infra/uninstall-consul.sh
install -D -m 755 ./iac/provider-gcp/nomad-cluster/scripts/uninstall-nomad.sh %{buildroot}/opt/e2b-infra/uninstall-nomad.sh
install -D -m 755 ./iac/provider-gcp/nomad-cluster/scripts/start-api.sh %{buildroot}/opt/e2b-infra/start-api.sh
install -D -m 755 ./iac/provider-gcp/nomad-cluster/scripts/start-client.sh %{buildroot}/opt/e2b-infra/start-client.sh
install -D -m 755 ./iac/provider-gcp/nomad-cluster/scripts/start-server.sh %{buildroot}/opt/e2b-infra/start-server.sh
install -D -m 755 ./iac/provider-gcp/nomad/jobs/deploy.sh %{buildroot}/opt/e2b-infra/deploy.sh
install -D -m 755 ./.github/actions/host-init/init-client.sh %{buildroot}/opt/e2b-infra/init-client.sh
install -D -m 644 ./iac/provider-gcp/nomad/jobs/env.template %{buildroot}/opt/e2b-infra/env.template
install -D -m 755 ./iac/provider-gcp/nomad-cluster/scripts/run-consul.sh %{buildroot}/opt/e2b-infra/run-consul.sh
install -D -m 755 ./iac/provider-gcp/nomad-cluster/scripts/run-nomad.sh  %{buildroot}/opt/e2b-infra/run-nomad.sh
cp -a ./packages/db/migrations %{buildroot}/opt/e2b-infra/bin/migrations
cp -a ./packages/clickhouse/migrations %{buildroot}/opt/e2b-infra/bin/migrations-clickhouse
cp -rp  %{_builddir}/e2b-deploy/* %{buildroot}/opt/e2b-infra/

%ifarch x86_64
    cp %{SOURCE3} %{buildroot}/opt/e2b-infra/bin/goose
%endif
%ifarch aarch64
    cp %{SOURCE2} %{buildroot}/opt/e2b-infra/bin/goose
%endif
chmod +x %{buildroot}/opt/e2b-infra/bin/goose

%ifarch aarch64
    install -D -m 755 %{SOURCE4} %{buildroot}/opt/e2b-infra/bin/vmlinux.bin
    install -D -m 755 %{SOURCE6} %{buildroot}/opt/e2b-infra/bin/vmlinux.bin.openeuler
    install -D -m 755 %{SOURCE8} %{buildroot}/opt/e2b-infra/bin/firecracker
%endif
%ifarch x86_64
    install -D -m 755 %{SOURCE5} %{buildroot}/opt/e2b-infra/bin/vmlinux.bin
%endif
install -D -m 755 %{SOURCE7} %{buildroot}/opt/e2b-infra/patch_e2b.py
cp -rp helm %{buildroot}/opt/e2b-infra/
chmod -R a+rX %{buildroot}/opt/e2b-infra/helm

%files
%license LICENSE
%doc README.md

# 二进制 & Dockerfile
/opt/e2b-infra/bin/*
# 扁平脚本 & 配置
/opt/e2b-infra/install-consul.sh
/opt/e2b-infra/install-nomad.sh
/opt/e2b-infra/nomad.service
/opt/e2b-infra/uninstall-consul.sh
/opt/e2b-infra/uninstall-nomad.sh
/opt/e2b-infra/start-api.sh
/opt/e2b-infra/start-client.sh
/opt/e2b-infra/start-server.sh
/opt/e2b-infra/deploy.sh
/opt/e2b-infra/init-client.sh
/opt/e2b-infra/env.template
/opt/e2b-infra/bin/goose
/opt/e2b-infra/run-consul.sh
/opt/e2b-infra/run-nomad.sh
/opt/e2b-infra/bin/migrations
/opt/e2b-infra/bin/migrations-clickhouse
/opt/e2b-infra/nomad/*.hcl
/opt/e2b-infra/bin/seed-db
/opt/e2b-infra/bin/vmlinux.bin
/opt/e2b-infra/helm/*
/opt/e2b-infra/patch_e2b.py
%ifarch aarch64
/opt/e2b-infra/bin/firecracker
%endif

/opt/e2b-infra/build.sh
/opt/e2b-infra/*.py
/opt/e2b-infra/dep/*
/opt/e2b-infra/dep/.env

%changelog
* Thu Jul 02 2026 Claude <noreply@anthropic.com> - 2026.09-4
- instrument acquire-wait (starting-slot queue) on the non-snapshot sandbox create path

* Fri Apr 24 2026 fly_1997 <flylove7@outlook.com> - 2026.09-3
- add auto-deploy

* Fri Mar 27 2026 zhourenjian <zhourenjian@huawei.com> - 2026.09-2
- adapt for 2026.09

* Sun Dec 07 2025 zhourenjian <zhourenjian@huawei.com> - 2025.36-1
- Adapt E2B to the arm64 architecture, remove its dependency on GCP, and add a one-click local installation script.Fix start-client process, use dnsmasq replace systemd-resolved, use paramater to decide current client belongs to which node pool, use glibc busybox,use provision.sh to install vm's deps while building template.Add arm vmlinux.bin and change x86 vmlinux.bin name.Make start-client process local.use dnsmasq replace systemd-resolved. Use paramater to decide current client belongs to which node pool. fix configure.sh run fail because busybox has no bash.change async to sync.complete readme.

