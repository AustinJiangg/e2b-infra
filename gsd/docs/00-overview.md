# 00 · 导读

## 本章目标

读完本章，你应当能够：

- 用两三句话说清 gsd 的 checkpoint / restore 是什么、回滚哪些东西、不回滚哪些东西；
- 说出全书六个部分各讲什么、前后怎么依赖，从而决定自己从哪里开始读；
- 说清它与整机级 checkpoint / restore（另一本手册）是什么关系；
- 拿着"一次 checkpoint、一次 restore"这条主线，知道每一步的原理和实现分别在哪一章；
- 知道这套代码现在处于什么状态：在哪台机器上验证过、哪些平台没有测、已知的限制和缺口去哪里查。

---

## 1. 这本书讲什么

### 1.1 一句话

**在沙箱不停机的前提下，把沙箱里的用户进程和根文件系统退回过去某一时刻。**

做这件事的是一个叫 gsd（Guest Sandbox Daemon）的守护进程。它以 root 身份运行在每个沙箱（Firecracker 虚拟机）**内部**，
监听 49984 端口，对外提供四个接口，挂在 Python SDK 的 `sandbox.checkpoint` 下：

- **checkpoint**（`create`）：把此刻的用户进程和根文件系统记下来，得到一个 checkpoint ID；
- **restore**：把用户进程和根文件系统放回某个 checkpoint 那一刻；
- **list / delete**：列出、删除本沙箱的 checkpoint。

全书把 restore 也叫"回滚"，两个词是同一个意思。

实现上它把一次快照拆成两半，各用一种机制：

- **进程那一半用 CRIU**。CRIU 是一个用户态工具，能把一组运行中的进程连同内存、打开的文件、寄存器保存成镜像文件，
  之后再按镜像把它们原样建回来。gsd 对沙箱里的每一棵用户进程树各调一次 CRIU，并利用内核的 soft-dirty 机制做增量：
  第二次起只保存上一次以来改过的内存页（原理见 [02](02-criu-and-incremental-memory.md)）。
- **文件那一半用 overlayfs 热切层**。沙箱的根文件系统是一个 overlay：只读的基础镜像在下，一个可写层在上。
  checkpoint 时把当前可写层原地变成只读层、换上一个新的空可写层；restore 时把整个层栈切回 checkpoint 时的样子。
  切换通过一个自定义的内核 ioctl 完成，根文件系统不卸载，也不搬数据（机制见 [06](06-overlay-hot-switch.md)）。

所以这套方案由四块组成，同在一个代码仓库里：guest 内的 **gsd 守护进程**、打了两个补丁的 **guest 内核**、
建模板时把 gsd 装进沙箱的 **orchestrator 注入逻辑**，以及 **Python SDK**。Firecracker 本身没有改动。

### 1.2 它解决什么问题

一个 AI Agent 在沙箱里执行多步任务：装依赖、改配置、跑构建、跑测试。某一步把环境搞坏了，它想回到上一步结束时的状态，
换个做法重试。对很多这样的任务来说，值得保存的状态只有两样：**用户自己起的进程**，和**文件**。
内核里的状态、系统服务的状态、没写完的页缓存，任务并不关心。

gsd 正是按这个范围做的：只保存 uid 1000 的用户进程和 overlay 根文件系统，别的一概不碰。
范围小带来两个直接后果，贯穿全书：

- **拍得少**：一次 checkpoint 写下的只是用户进程改过的内存页；文件那一半只是一次层切换，数据早就在可写层里，不需要再搬一遍
  （唯一的例外是 checkpoint 那一刻正以可写方式打开着的文件，要拷贝一份，见 [07](07-overlay-concurrency-and-fixes.md)）；
- **回滚要重建进程**：restore 不能把内存"写回"一个活着的进程，只能把当前的用户进程全部杀掉，再由 CRIU 按镜像重新建出来。
  所以 restore 的耗时主要取决于被恢复的进程有多大，而不是这一步改了多少。

问题的完整定义和两条技术路线的比较见 [01](01-problem-and-scope.md)。

### 1.3 适用与不适用

**适用**：

- Agent 或自动化任务在沙箱里逐步执行，需要"走坏了就退回上一步"，而要退回的状态就是用户进程加文件；
- 在同一个起点上尝试多种做法：restore 之后再 checkpoint 会形成分支，历史是一棵树（[13](13-state-concurrency-capacity.md)）；
- 沙箱里除了被回滚的用户进程之外，还有一些不希望跟着回滚的东西：envd、系统服务和内核都不在回滚范围里，restore 前后一直在运行。

**不适用**：

- **需要连内核状态一起回滚**：内核态、页缓存、root 起的系统服务都不在回滚范围里（[01](01-problem-and-scope.md)）；
- **进程用了 CRIU 保存不了的东西**：这样的进程 checkpoint 会失败，错误信息里带 CRIU 日志的尾部（[22](22-semantics-and-limits.md)）；
- **跨沙箱、跨宿主、长期保存**：镜像放在沙箱内部的内存文件系统里，层放在沙箱自己的根盘上，都随沙箱消失（[03](03-goals-and-tradeoffs.md)）；
- **x86 的沙箱**：目前只支持 arm64 的 guest（[03](03-goals-and-tradeoffs.md)）；
- **撤销对外部世界的影响**：已经发出去的请求、写进外部数据库的数据，任何快照都退不回来。

---

## 2. 与整机级手册的分工

同一个代码仓库里还有另一套 checkpoint / restore，由宿主上的 orchestrator 和分叉的 Firecracker 实现，
回滚的是**整台虚拟机**：全部内存、vCPU、设备状态和磁盘。它有自己的手册（`e2b-infra/rollback/docs/`）。
本书把它称为"整机级"，把 gsd 这一套称为"进程级"。

两者的 SDK 入口同名（都是 `sandbox.checkpoint`），请求都发往沙箱地址的 49984 端口，但实现完全不同：
整机级在宿主侧把请求截下、由 orchestrator 应答，沙箱里什么都不装；进程级的请求一路进到沙箱里，由 gsd 应答。
它们回滚的范围不同、成本模型不同、依赖的东西也不同，不是同一件事的两种实现。
逐项对比、各自适合什么场景、能不能在同一个沙箱上一起用，只在 [18](18-process-vs-machine-level.md) 讲，别的章不重复。

---

## 3. 六个部分，以及它们怎么衔接

| 部分 | 章 | 回答的问题 |
|---|---|---|
| 一 基础 | [01](01-problem-and-scope.md)–[03](03-goals-and-tradeoffs.md) | 要回滚的是什么？CRIU 和增量内存的原理是什么？目标、硬约束和取舍是什么？ |
| 二 设计与实现 | [04](04-architecture.md)–[11](11-end-to-end.md) | 各部件怎么分工？沙箱怎么开机、gsd 怎么进到模板里？层怎么热切换？进程树怎么认定？checkpoint 和 restore 各自一步步做了什么？ |
| 三 工程保障 | [12](12-failure-semantics.md)–[15](15-known-limits.md) | 出错时沙箱处于什么状态？状态怎么持久化、并发和容量怎么管？交接时的问题怎么处理的？还有哪些已知限制？ |
| 四 性能 | [16](16-restore-slowness-case.md)–[18](18-process-vs-machine-level.md) | restore 当初为什么慢、怎么查到根因？修复之后时间花在哪？与整机级怎么比？ |
| 五 验证与实测 | [19](19-testing-and-debug-kernel.md)–[20](20-results-and-gaps.md) | 怎么证明它是对的？实测数字是多少？还有哪些没测？ |
| 六 使用与运维 | [21](21-quickstart.md)–[26](26-acceptance-runbook.md) | 怎么调用、语义边界在哪？怎么部署、配置、观测、排障、验收？ |
| 附录 | [A](A-glossary-and-code-map.md)、[B](B-extending.md) | 术语、代码地图、文件格式；继续开发时不能破坏什么 |

各部分之间的依赖如下（"←"读作"读它之前应先读"）：

- **第一部分是一切的前提。** [01](01-problem-and-scope.md) 划定回滚范围；[02](02-criu-and-incremental-memory.md) 讲清 CRIU、soft-dirty
  和"透明大页必须关"这三件后面反复用到的事；[03](03-goals-and-tradeoffs.md) 把目标、硬约束和四项取舍列全。
  第二部分的每一个设计决定都能在 03 找到出处。
- **第二部分是主体，内部也有顺序。** [04](04-architecture.md) 给出组件、请求路径和目录布局（目录与层的布局表全书只在那里）；
  [05](05-guest-boot-and-injection.md) 讲沙箱开机时根文件系统是怎么搭起来的，这是 06 的前提；
  [06](06-overlay-hot-switch.md)、[07](07-overlay-concurrency-and-fixes.md) 讲文件那一半（06 讲机制，07 讲并发、生命周期和内核侧的修复）；
  [08](08-process-trees.md) 讲进程那一半的基础——什么算一棵用户进程树；
  [09](09-checkpoint.md)、[10](10-restore.md) 把两半合起来，分别是 checkpoint 和 restore 的逐步实现；
  [11](11-end-to-end.md) 把两条路径从头到尾走一遍。
- **第三部分 ← 第二部分。** 它讲的是"出错时怎么不坏事"：失败语义（[12](12-failure-semantics.md)）、
  状态持久化与并发、容量（[13](13-state-concurrency-capacity.md)）都大量引用第二部分的机制，反过来不成立。
  [14](14-issue-resolution.md) 是给评审和接手者的修复总账，[15](15-known-limits.md) 是全书唯一的已知限制表。
- **第四部分 ← 02、06、09、10。** [16](16-restore-slowness-case.md) 和 [17](17-performance-after-fixes.md) 是两个案例：
  前者讲交接时 restore 为什么慢，后者讲修复引入的回退怎么收回、现在的代价模型是什么；
  [18](18-process-vs-machine-level.md) 把进程级和整机级放在一起比较。
- **第五部分给证据。** [19](19-testing-and-debug-kernel.md) 讲测试体系和调试内核，[20](20-results-and-gaps.md) 给数字。
  **判定性的实测数字只在 20 章**；其他章举数时都当场标明平台、日期、代码版本和口径，只为说明原理，不能与 20 章的数相减。
- **第六部分面向使用者和运维**，可以不读前五部分直接上手，每一节都链接回对应的原理章节。

---

## 4. 从哪里读起

| 你是谁 | 建议的顺序 | 读完能做什么 |
|---|---|---|
| 在沙箱上写业务的开发者 | [21](21-quickstart.md) → [22](22-semantics-and-limits.md) → [12](12-failure-semantics.md) | 调通接口；知道什么会回滚、什么不会；知道每种错误之后沙箱处于什么状态、该怎么办 |
| 运维 | [23](23-deployment.md) → [24](24-configuration-and-observability.md) → [25](25-troubleshooting.md) → [26](26-acceptance-runbook.md) | 部署、建模板、读日志和状态文件、按症状排障、上机验收 |
| 评审 | [03](03-goals-and-tradeoffs.md) → [12](12-failure-semantics.md) → [15](15-known-limits.md) → [19](19-testing-and-debug-kernel.md) → [20](20-results-and-gaps.md) | 判断设计取舍是否成立、失败时是否安全、还有哪些限制和没测到的地方 |
| 接手开发的人 | 第一部分到第三部分按顺序读（[01](01-problem-and-scope.md)–[15](15-known-limits.md)）→ [B](B-extending.md) | 能改代码，并且知道哪些不变量不能破坏、改完怎么验证 |

只想知道"它和整机级该用哪个"的读者，读 [01](01-problem-and-scope.md) 和 [18](18-process-vs-machine-level.md) 两章就够了。

---

## 5. 主线：一次 checkpoint、一次 restore 各经过哪几章

全书可以看成是在反复放大这两条路径上的某一步。先把路径列出来，读每一章时知道自己在哪一步。

### 5.1 一次 checkpoint

| # | 这一步做什么 | 原理 / 实现在哪 |
|---|---|---|
| 1 | SDK 把请求发到沙箱地址的 49984 端口，带上沙箱的 access token | 用法 [21](21-quickstart.md)；请求路径 [04](04-architecture.md) |
| 2 | 宿主的代理把请求转进沙箱；gsd 校验 token | [04](04-architecture.md) |
| 3 | gsd 取串行锁，检查自己是否处于可服务状态 | [13](13-state-concurrency-capacity.md)、[12](12-failure-semantics.md) |
| 4 | 扫描一次 `/proc`，找出全部用户进程树的根，记下每个进程此刻是否停着 | [08](08-process-trees.md) |
| 5 | 预检：树与树之间有没有共用的管道或 socket；镜像放不放得下 | [08](08-process-trees.md)、[13](13-state-concurrency-capacity.md) |
| 6 | 对每棵树各做一次 CRIU dump，做完的树停在原地；能做增量的树只写脏页 | 原理 [02](02-criu-and-incremental-memory.md)；实现 [09](09-checkpoint.md) |
| 7 | 所有树都停住之后，把当前可写层沉成只读层，换上新的空可写层 | 机制 [06](06-overlay-hot-switch.md)；并发与内核侧保证 [07](07-overlay-concurrency-and-fixes.md) |
| 8 | 给刚才保存过的进程重新清一次 soft-dirty，再让它们继续运行 | 为什么要重清 [02](02-criu-and-incremental-memory.md)；实现 [09](09-checkpoint.md) |
| 9 | 记录这个 checkpoint，把状态写进状态文件，把各阶段耗时随响应返回 | [13](13-state-concurrency-capacity.md)、[24](24-configuration-and-observability.md) |
| 10 | 任何一步失败：放开已停住的进程，删掉这次的镜像，沙箱回到原状 | [12](12-failure-semantics.md) |

### 5.2 一次 restore

| # | 这一步做什么 | 原理 / 实现在哪 |
|---|---|---|
| 1 | 请求进到 gsd、校验 token、取串行锁，找到目标 checkpoint | [04](04-architecture.md)、[13](13-state-concurrency-capacity.md) |
| 2 | 杀进程之前把能查的都查完：每棵树的镜像和它依赖的父镜像都在；切层要用的目录建好 | [10](10-restore.md) |
| 3 | 扫描当前的用户进程 | [08](08-process-trees.md) |
| 4 | **把当前的用户进程全部杀掉**（先冻住、确认没有新进程冒出来、再杀）。这是 restore 的提交点 | [08](08-process-trees.md)、[10](10-restore.md) |
| 5 | 把 overlay 的层栈切回目标 checkpoint 的样子，换上全新的可写层 | [06](06-overlay-hot-switch.md)、[07](07-overlay-concurrency-and-fixes.md) |
| 6 | 按保存时的顺序，对每棵树各做一次 CRIU restore；恢复出来的进程用原来的 PID 继续运行 | [02](02-criu-and-incremental-memory.md)、[10](10-restore.md) |
| 7 | 记下"下一次增量以这个 checkpoint 为父"，写状态文件，后台回收不再需要的目录 | [10](10-restore.md)、[13](13-state-concurrency-capacity.md) |
| 8 | 失败时分两种：第 4 步之前失败，什么都没动；第 4 步之后失败，进程已经没了，沙箱进入降级状态，只能再 restore 一次 | [12](12-failure-semantics.md) |

把这两条路径完整走一遍、并标出每个计时字段落在哪一步的是 [11](11-end-to-end.md)。

有一点值得先记住：文件系统和进程是在同一段"进程全部停着"的窗口里拍下的（checkpoint 的第 6～8 步），
所以镜像里的进程状态和沉下去的文件系统是同一时刻的；但两次都没有暂停虚拟机，沙箱里的其他进程一直在运行。
这既是"不停机"的含义，也是 [07](07-overlay-concurrency-and-fixes.md) 一整章存在的原因：
层在切换的时候，那些不归 gsd 管的进程可能正在读写文件。

---

## 6. 这本书和代码的关系

这套代码是分两个阶段形成的。交接前有一版能跑通主路径的实现；接手后先通读代码，整理出一份 36 条的问题清单，
再按清单逐批修复，并在现场验证中又发现和修掉了一批清单之外的内核问题。最后的结果是原实现之上的 28 个提交，
加上一个合并了全部内核侧修复的补丁（`0003-overlayfs-hot-switch-fixes.patch`，原始的热切层补丁
`0002-overlayfs-hot-switch-checkpoint.patch` 保持原样）。

**读者不需要知道这个过程。** 除了下面三处，全书只写现状：

- [14](14-issue-resolution.md)：问题清单每一组的处理结果，以及几项方向性决定的理由；
- [16](16-restore-slowness-case.md)、[17](17-performance-after-fixes.md)：两个性能案例，保留了"先怀疑什么、后来推翻什么"的过程；
- [07](07-overlay-concurrency-and-fixes.md)：每个主题的"根因"小节。

书里说"交接前的实现""修改前""修复后"时，指的就是这两个阶段。

书中的代码引用以 `deltabox-gsd` 分支的提交 `259f1a25c` 为准，写成"函数名（文件:行号）"的形式。
gsd 守护进程的核心包路径较长，统一简写为 `ckpt/`，即 `packages/gsd/internal/services/checkpoint/`；
内核补丁只引用补丁文件名和函数名。完整的代码地图在 [A](A-glossary-and-code-map.md)。

---

## 7. 当前状态

| 项 | 状态 |
|---|---|
| 代码版本 | `deltabox-gsd@259f1a25c`（运行部件与 10-08 完整验收对应这个提交）。当前 SDK 使用后续提交 `dcca61266`：带入口流量令牌的沙箱上给 gsd 的请求加令牌头（[04](04-architecture.md)） |
| 新 920B（192.168.45.47） | 2026-10-08 整包重新编译、重建模板，功能验证、多棵进程树验证、soft-dirty 竞态验证、性能测试和四组压测全部通过；2026-10-09 按运行时布局复核通过；2026-10-16 新版独立分档性能三轮通过，18 次恢复内容校验正确（[20](20-results-and-gaps.md)） |
| 950 | 未部署、未测 |
| guest 架构 | 只支持 arm64（原因见 [03](03-goals-and-tradeoffs.md)） |
| Firecracker | gsd 不需要对 Firecracker 做任何改动 |
| 已知限制 | 一张表，在 [15](15-known-limits.md)。其中"设计如此"的每一项都能在 [03](03-goals-and-tradeoffs.md) 找到对应的取舍 |
| 没有测到的 | 缺口表在 [20](20-results-and-gaps.md)：950、更大规格的沙箱、三棵以上的进程树、小时级的长时间运行等 |

使用上最容易踩到的几条边界，先在这里各提一句，细节都在 [22](22-semantics-and-limits.md) 和 [15](15-known-limits.md)：

- **被回滚的只有用户进程和 overlay 根文件系统。** `/run`、`/dev/shm`、`/var/log` 不回滚；`/tmp` 回滚。
- **每个后台命令是一棵独立的进程树。** 树和树之间如果用管道或 socket 相连，checkpoint 会被拒绝；
  需要严格一致的一组进程应当由同一个命令启动，成为一棵树。
- **镜像占的是沙箱自己的内存。** gsd 在保存之前先估算，放不下时直接拒绝并给出数字，而不是写到一半把沙箱的内存耗尽。
- **restore 一旦开始杀进程就没有回头路。** 之后再失败，沙箱里的用户进程已经没有了，要再成功 restore 一次才能恢复到一个完整的状态。
- **模板里固化了建模板时的内核和 gsd。** 换了内核或 gsd 之后必须重建模板，已有模板不会跟着变（[05](05-guest-boot-and-injection.md)、[23](23-deployment.md)）。

下一章（[01](01-problem-and-scope.md)）从问题本身讲起：进程级回滚到底回滚什么、不回滚什么，它和整机快照是两条怎样不同的路线。

---

## 本章要点

1. gsd 是运行在沙箱内部的守护进程，在**不停机**的前提下把**用户进程和根文件系统**退回过去某一时刻：
   进程用 CRIU 加 soft-dirty 增量，文件用 overlayfs 热切层。
2. 回滚范围小是它的全部特点的来源：checkpoint 拍得少、文件那一半几乎不花时间；restore 必须杀掉并重建进程，耗时取决于进程有多大。
3. 全书六部分：基础 → 设计与实现 → 工程保障 → 性能 → 验证与实测 → 使用与运维。第一部分是前提；判定性的数字只在 20 章；
   问题总表只在 14 章，已知限制只在 15 章，与整机级的对比只在 18 章。
4. 一次 checkpoint 与一次 restore 的每一步都能在 §5 的两张表里找到对应章节，[11](11-end-to-end.md) 把它们完整走一遍。
5. 与整机级是两套独立的机制，入口同名、范围和成本模型都不同，对比和选型见 [18](18-process-vs-machine-level.md)。
6. 代码以 `deltabox-gsd@259f1a25c` 为准，已在新 920B 上完成确认性验证；950 未部署、未测；只支持 arm64 的 guest。
