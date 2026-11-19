# lamarck 面试讲解手册（中文）

> 用法：按下面的顺序读代码，每个文件分三层看——**(a) 骨架**（这段代码在系统里干什么、数据怎么流），
> **(b) 为什么**（为什么这样设计、放弃了什么方案），**(c) 语法**（读懂代码需要的 Python 细节）。
> 力气花在 (a)(b)，(c) 遇到再查。后半部分是约 35 道面试题，答案全部基于本仓库的代码和实测结果。

---

## 0. 一句话讲清项目

lamarck 让一个 LLM「harness 工程师」（proposer）反复改写另一个 LLM agent 的 harness（提示词、配置、
控制代码），在沙箱里评测，再由 selector 挑选；同时把每次被选中的修改按「根因」记进
**Self-Improvement Memory (SIM)**，当换数据集或换模型继续优化时，把这份记忆带过去。
论文来源：SIM（Yu et al., arXiv 2611.18856）和 RRSI（Xia et al., arXiv 2609.24972）。
工程上的三块：进化引擎 + SIM、gVisor 沙箱评测层、带预算和审计的 LLM 网关。

---

## 1. 阅读顺序（共 18 个文件）

### 第 1 站 `lamarck/types.py`（数据长什么样）
- **骨架**：`TaskOutcome`（一道题的得分、token、调用次数、给 proposer 看的 trace）和 `EvalResult`
  （一个 harness 在一个 split 上的整份成绩）。`score` = 平均 reward，`cost` = 每题平均 token。
- **为什么**：先把「分数」和「成本」定义成同一个对象上的两个属性，后面 RRSI 的 cost rule 直接用它们；
  失败的题记 0 分但不从分母里删，防止「把难题搞崩溃反而分数变高」。
- **语法**：`@dataclass`、`@property`、`from __future__ import annotations`（类型注解延迟求值）。

### 第 2 站 `lamarck/config.py`
- **骨架**：`RunConfig` 把论文里的符号（rounds N、k、b_min/b_max、δ、β0/β1、w_s/w_c/w_n）和工程参数放一起。
- **为什么**：每个正则项都有开关（`use_noise_floor` 等），方便做消融；`memory` 字段决定四种条件。
- **语法**：`Literal["none", "problems", "full", "retrieved"]`、`__post_init__` 做校验。

### 第 3 站 `lamarck/engine/gitops.py`（候选就是 git 分支）
- **骨架**：每个 run 一个小 git 仓库，只有 `harness/`。incumbent = `evolve` 分支的 tip；第 t 轮第 j 个候选
  在自己的 worktree + 分支 `cand/r<t>s<j>` 上起草；接受 = fast-forward。迁移时 `clone_from` 把源 run 的
  整个历史克隆过来，所以谱系（lineage）跨迁移保留。
- **为什么**：git 天然提供「内容哈希」（tree hash 当缓存 key）、隔离（worktree）、审计（log）。RRSI 也这么做，
  这里是自己重写的简化版。
- **语法**：`subprocess.run(capture_output=True, text=True)`；`git rev-parse <ref>:harness` 取子树哈希。

### 第 4 站 `lamarck/engine/regularizers.py`（RRSI 的正则项，纯函数）
- **骨架**：`edit_budget`（余弦退火的编辑预算）、`calibrate_delta`（同一 harness 重复评测估噪声带 δ）、
  `judge`（noise floor → cost rule → guards 三道关）、`prune_set`。
- **为什么**：纯函数好测；selection 的每一条拒绝理由都写成字符串存进历史，审计时能看懂为什么没选。
  这里有个真实踩坑：温度 0 下重复评测仍然差 4/60 题，2σ 的 δ 有 12.5 分，导致 21 个候选全被
  cost rule 拒绝（见 PREREG 偏差 2），最后改成 1σ、β1=40、w_c=5。
- **语法**：`math.ceil(round(v, 9))` 防浮点误差；`statistics.stdev`。

### 第 5 站 `lamarck/sim/memory.py`（核心：SIM）
- **骨架**：`SIM{next_id, carried_from, aliases, problems[]}`，`Problem{id, name, opened_round, attempts[]}`，
  `Attempt{round, sample, accepted, backfilled, what, files, predicted, fixed, missed, passed, total, domain}`
  ——逐字段对应论文 App. D.3。`update()` = 论文的 U(M_t, e_t)：先看 proposer 引用的 id（跟随 alias），
  再看是否有完全相同的根因文本，都没有就新建 P<next_id>。`merge_identical()` 确定性合并、保留全部 attempts、记录 alias。
- **为什么**：论文 D.1 明确说「不用相似度模型，由 proposer 自己决定是否匹配；文本完全相同才合并」。
  我们照做，并用 property test 证明不变量（每次 update 恰好 +1 个 attempt、合并无损/幂等/确定）。
- **语法**：`copy.deepcopy`、`dataclasses.asdict`、`re.sub(r"\s+", " ", ...)` 归一化。

### 第 6 站 `lamarck/sim/prompts.py` 与 `lamarck/sim/retrieval.py`
- **骨架**：四种条件下 proposer/selector 看到什么：`none` 什么都没有；`problems` 只有 id+根因；`full` 完整 JSON +
  App. D.2 的 8 条使用指南；`retrieved`（扩展）用 BM25 + 向量相似度做 RRF 融合，取 top-k 且不超过 token 预算。
- **为什么**：Problem-Only 是论文的关键对照：只保留诊断、去掉修改和结果，用来回答「完整记录是否比只有根因更有用」。
- **语法**：BM25 公式手写；`numpy` 点积算余弦；RRF = Σ 1/(60+rank)。

### 第 7 站 `lamarck/engine/proposer.py` 与 `selector.py`
- **骨架**：proposer 每个候选一次调用，输入 harness 全部文件、evolve 反馈（失败题 + trace）、本 run 历史 D_t、
  prune set、SIM；输出 `<manifest>` JSON（根因引用/新根因、修改描述、预测会修好的题）+ 逐字 `<file>` 块。
  selector 两次调用：评测前 screen（拒绝泄漏、空改动、钻评分器空子、未声明的打包修改、无界循环），评测后 select
  （只能在规则允许的候选里挑或选 none）。
- **为什么**：最初让模型把代码转义进 JSON，长代码经常截断/转义错；改成「JSON 头 + 原文文件块」后解析稳定。
  selector 不能选规则不允许的候选：LLM 负责「判断」，硬规则负责「安全边界」。
- **语法**：手写括号匹配的 `extract_json`（能容忍字符串里的大括号）；正则 `re.S`。

### 第 8 站 `lamarck/engine/critic.py`
- **骨架**：评测前的确定性筛查：只看 diff 的「新增行」匹配泄漏模式（题号、数据库名、实体 id）；Python 文件必须能编译；
  import 白名单；不能调用 `open/exec/eval`；声明的独立修改数 ≤ b_t。
- **为什么**：便宜的检查放在花钱的评测之前；只看新增行，避免因为 incumbent 里已有的文字误伤。
- **语法**：`ast.parse` + `ast.walk` 找 `Import/ImportFrom/Call`。

### 第 9 站 `lamarck/engine/loop.py`（把一切串起来）
- **骨架**：`EvolutionRun.round(t)`：评测 incumbent → 并行 k 个提议（每个在自己的分支提交，manifest 先落盘 = 预测先封存）
  → critic + LLM screen → 并行沙箱评测 → judge + LLM select → 写历史、更新 SIM、fast-forward → 存状态。
  `create()` 处理四种条件：从种子开始，或从源 run 克隆并按条件携带 SIM。
- **为什么**：可恢复性是硬需求（实验跑几小时）：已封存的提议、screen/select 结果、评测都会被复用，恢复不花额外调用；
  SIM 更新是幂等的。held-out 分数写进一个没有任何 prompt 会读的文件。
- **语法**：`ThreadPoolExecutor`、`threading.Lock`（git worktree 操作不能并发）、`dataclass(field(default_factory=...))`。

### 第 10 站 `lamarck/engine/evaluator.py`
- **骨架**：把 harness 和「只含公开字段」的任务写进 job 目录，交给沙箱后端，回来后在宿主机用金标准打分；
  结果按（domain, split, model, harness 树哈希, rollout）缓存。
- **为什么**：金标准永远不进沙箱；缓存让多个条件共享同一个起点 harness 的评测（公平且省钱）。

### 第 11 站 `lamarck/gateway/`（ledger.py → ratelimit.py → providers.py → server.py → client.py）
- **骨架**：沙箱和宿主机的搜索角色都只调用网关。网关按「每个 run 一张 grant」鉴权（角色+模型范围+预算），
  全局硬上限（policy 80,000 / search 3,000 次）、每 run/角色预算、令牌桶限流、上游重试（每次重试都计入预算）、
  账本、审计日志（只存请求哈希，不存内容和 key）。
- **为什么**：key 只在网关进程里；「先 reserve 再调用」保证并发下也不会超额（有并发测试）。
  一个真实 bug：标准库 `ThreadingHTTPServer` 的 listen backlog 默认 5，16 并发时连接被 reset，改成 512 并加客户端重试。
- **语法**：`BaseHTTPRequestHandler`、`secrets.compare_digest`（防时序攻击）、`fcntl` 思路的原子写（tmp + rename）。

### 第 12 站 `lamarck/sandbox/`（runner.py → runtime_util.py → backends.py → docker_backend.py → probes.py → image.py）
- **骨架**：每个评测 job 一个全新容器（gVisor runsc），只读根文件系统、只读 harness/job/data 挂载、唯一可写 /out，
  nobody 用户、去掉全部 capability、CPU/内存/pid 限制、宿主机超时；网络只接 `--internal` 的 lamarck-sbx，
  上面只有网关。预热池按挂载组合维护，容器只用一次。镜像按白名单逐文件组装，测试保证没有题目/金标准进镜像。
- **为什么**：harness 代码是 LLM 写的，必须当作不可信代码。gVisor 的网络栈访问不到 Docker 内置 DNS(127.0.0.11)，
  所以用 `--add-host` 注入网关地址——副作用是沙箱里根本没有 DNS，外部域名天然解析不了。
  escape probe 实测：外网 IP、DNS、元数据地址、宿主机、写只读挂载、docker.sock、raw socket 全部失败，只有网关可达。
- **语法**：`docker run/exec` 参数；`os.umask(0)`（virtiofs uid 映射下文件要能被宿主机读）。

### 第 13 站 `lamarck/domains/base.py` + `text2sql/` + `supportops/`
- **骨架**：Domain 接口：`tasks(split)` 返回 public/private 两部分；`data_mounts(split)` 只挂这个 split 的数据；
  `runtime` 在沙箱里跑；`score()` 在宿主机打分；`guards()` 是不可补偿约束（support-ops：越权写不能增加）。
  text2sql：Spider（源）→ BIRD（目标），三层只读 SQLite（ro+immutable URI、query_only、authorizer），执行准确率。
  support-ops：从作者自己的 agent-eval-mcp 拷来的 MCP 服务 + 有界 agent loop + 110 道题（重放金轨迹 110/110 通过）。
- **为什么**：任务公开字段和私有字段在类型层面分开，「金标准不进沙箱」就成了接口性质，而不是每个适配器自己记得。

### 第 14 站 `lamarck/experiments/plan.py`、`stats.py`、`report.py`、`cli.py`
- **骨架**：PREREG.md 里的实验计划写成代码（每个 run 的配置 + 依赖），调度器按依赖并行跑；`stats.py` 做配对 t 检验、
  精确符号秩检验（枚举全部 2^n 种符号）、95% CI、d_z——并且用论文 Table 3 的数据复现出论文的统计量；
  `report.py` 从 run 目录生成 results/ 和 RESULTS.md，文档里每个数字都能追溯到文件。

### 第 15 站 `lamarck/dashboard/`
- **骨架**：一个 HTML 文件、零运行时依赖：谱系树、每轮 held-out 曲线、SIM 浏览器、成本、路线图时间轴（进度线随滚动增长，
  停在最后一个已完成节点）。

---

## 2. 面试问答（约 35 题）

（见下文，结果相关的数字全部来自 results/ 下的文件。）
