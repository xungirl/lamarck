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


结果相关的数字全部来自 `results/` 下的文件（`results/RESULTS.md` 是生成的总表）。

### A. 项目定位

**Q1. 一句话：lamarck 做了什么？**
一个「改 harness 的 agent」：proposer 读当前 harness 的代码和失败轨迹，提出修改；修改在 gVisor 沙箱里评测；
selector 在硬规则允许的候选里挑一个；被接受的修改按根因写进 SIM。换数据集（Spider → BIRD）或换业务策略
（support-ops 标准策略 → 改过的策略）时，把 harness 和 SIM 一起带过去继续优化。

**Q2. 为什么叫 lamarck？**
拉马克主义：后天获得的性状可以遗传。这里「后天经验」= SIM 里记下的根因、修改、预测和结果，
在换任务时「遗传」给下一轮优化——这正是论文要检验的假设。

**Q3. 和论文的关系？是复现吗？**
论文（SIM, arXiv 2611.18856）没放代码。这是独立实现 + 预算受限的小规模实验，不是严格复现：
模型不同（qwen3.8-flash 一个模型担任所有角色）、轮数和任务数都缩小了（PREREG 偏差 3）。
README 和 REPORT 都写明了这一点；也写明了 GitHub 上已经有两个受 SIM 启发的项目，所以不声称「第一个」。

**Q4. 什么是 harness？**
模型之外的一切：system prompt、few-shot、控制流代码（比如生成 SQL 后先执行、空结果再修一次）、配置。
模型固定，只改 harness，这样分数变化能归因到 harness。

### B. 引擎和 RRSI 正则项

**Q5. 候选为什么是 git 分支？**
tree hash 天然是内容寻址的缓存 key；worktree 让多个候选并行起草互不干扰；接受 = fast-forward，
谱系（lineage）就是 git log，迁移时 clone 整个仓库谱系也跟着走。

**Q6. noise band δ 是什么，为什么需要？**
温度 0 下同一个 harness 重复评测分数也会变（实测 spider_evolve 三次 78.33/85.00/78.33）。
δ 由起始 harness 的 3 次评测标准差算出；提升小于 δ 的候选视为「噪声内」，走另一条更严格的规则，防止把运气当进步。

**Q7. 偏差 2 讲了什么故事？**
最初 δ 用 2σ，结果 12.5 分的带宽让所有候选都落在带内，再被 token 成本规则全部拒绝（21 个候选 0 个可接受），
源 run 产生不了任何修改，也就没有 SIM。改成 1σ、β1=40（RRSI 默认）、w_c=5。这个决定只用了源 run 的接受统计，
没看任何目标或 held-out 结果——这是预注册里「偏差要在看结果前做、并写清楚」的意义。

**Q8. edit budget 为什么要退火？**
早期允许一次改 3 处（探索），后期只允许 1 处（精修）；也限制了「一次打包很多修改、说不清哪个起作用」的问题，
让 SIM 里每条记录的因果更干净。

**Q9. critic 和 LLM screen 的分工？**
critic 是确定性的、便宜的：只看 diff 新增行里有没有题号/数据库名泄漏、Python 能不能编译、import 白名单、
有没有 open/exec/eval。LLM screen 判断语义问题：钻评分器空子、未声明的打包修改、无界循环。便宜的先跑，省评测钱。

**Q10. held-out 为什么永远不进 prompt？**
否则 proposer 会对 held-out 过拟合，held-out 就不再是 held-out。代码上：held-out 分数写进一个没有任何 prompt
构造函数会读的文件，测试 `test_heldout_never_reaches_the_search` 检查所有 prompt 里没有 held-out 题号。

### C. SIM

**Q11. SIM 一条记录长什么样？**
`Problem{id, name(根因), attempts[]}`，`Attempt{round, what(改了什么), files, predicted(预测会修好的题),
fixed, missed, passed/total, domain(在哪个数据集/模型上)}`。按论文 App. D.3 字段一一对应。

**Q12. 「预测先封存」怎么保证？**
proposer 的 manifest（根因、预测的题）在评测开始前写盘，测试比较 manifest 的 `sealed_at` 和评测缓存文件的 mtime。
这样事后无法「改预测去贴合结果」。

**Q13. 为什么合并根因不用 embedding 相似度？**
论文 D.1：由 proposer 自己引用已有 id 来决定是否同一问题；只有文本完全相同才自动合并。确定性、可审计、无额外模型。
Hypothesis 属性测试证明：每次 update 恰好加一个 attempt，合并无损、幂等、确定。

**Q14. 四种条件分别是什么？**
Cold Start（种子 harness，无 SIM）、Harness Transfer（源 harness，无 SIM）、Problem-Only（源 harness + 只有根因）、
Full SIM（源 harness + 完整记录，并在目标上继续更新）。Problem-Only 用来回答「完整记录比只有诊断多带来什么」。

**Q15. retrieval 扩展做了什么？**
多个源 run 的 SIM 合并后很长，用 BM25 + 向量（bge-small，可选）做 RRF 融合，取 top-k 且不超过 token 预算，
对比「全量 SIM」和「检索 SIM」。

### D. 网关和钱

**Q16. 为什么所有调用都要过网关？**
key 只在网关进程内存里，沙箱里的不可信代码拿不到；每个 run 一张 grant（角色、模型、预算）；
全局调用上限、令牌桶限流、重试也计预算；审计日志只存请求哈希不存内容。

**Q17. RMB spend ledger 怎么做到「不超额」？**
调用前按最坏情况预留：请求每个字符算 1 个输入 token（BPE token 至少一个字符，所以是上界）+ max_tokens 个输出 token。
`已花 + 在途预留 + 本次最坏 > 上限` 就拒绝。调用后用 API 返回的 usage 结算（cached token 按缓存价）。
未知模型直接拒绝（fail closed）；超时没有 usage 就按最坏情况扣。一个进程用 flock 独占账本文件。

**Q18. 调度器的 money gate 是什么？**
按优先级顺序走所有待跑 run（包括还在等依赖的），每个 run 先「认领」自己的预估费用；钱只够高优先级的就不启动低优先级的。
这样不会出现「便宜的低优先级实验把钱花完，配对实验跑不完」。正在跑的 run 按「预估 − 已花」计入。

**Q19. 2026-11-19 的欠费事故讲一下。**
账户余额用完，DashScope 开始返回 403 AccessDenied.Unpurchased。当时的评测器把 403 当「这道题做错」，
几个 run 的 held-out 变成 0/120——测的是基础设施不是 harness。修复：运行时把网关/供应商错误单独记录，
评测器抛 InfraFailure（不缓存、不计分），run 暂停为 infra_error，可恢复；被污染的缓存文件改名留档。
`results/validity.json` 记录了第一个 403 的时间、每个 run 受影响的调用数，以及为什么只有 sql-src-4 是干净的。

**Q20. 为什么换成一个模型担任所有角色？**
预算只剩约 ¥12，qwen4-max 没有可查的价格，spend ledger 会 fail closed 拒绝。qwen3.8-flash 价格明确
（输入 ¥0.8/M、输出 ¥2.7/M、缓存 ¥0.1/M）。代价：proposer 变弱，这是结果解释时要考虑的混杂因素。

### E. 沙箱

**Q21. 沙箱的隔离层次？**
每个评测 job 一个新容器，gVisor runsc（系统调用进用户态内核）、只读根文件系统和挂载、唯一可写 /out、nobody 用户、
去掉全部 capability、CPU/内存/pid 限制、宿主机超时；网络只接 internal 网络，上面只有网关。

**Q22. gVisor 下 DNS 的坑？**
gVisor 的网络栈访问不到 Docker 内置 DNS（127.0.0.11），容器解析不了网关名。用 `--add-host` 注入网关 IP；
副作用是沙箱里根本没有 DNS，外部域名天然解析失败（escape probe 也验证了）。

**Q23. colima 重启后 gVisor 不见了？**
colima 每次启动会重写 VM 里的 /etc/docker/daemon.json，runsc 运行时注册丢失。后端检测不到 runsc 会回退到 runc
并记录在 job 结果里；所以复现脚本每次启动后检查并重新 `runsc install`。

**Q24. 金标准为什么永远不进沙箱？**
Domain 接口把任务分成 public/private 两部分，只有 public 写进 job 目录；打分在宿主机做。镜像按白名单组装，
测试保证没有题目和金标准进镜像。

### F. 实验设计和统计

**Q25. 为什么要预注册？**
实验成本高、轮数少、噪声大，最容易「看结果再调设置」。PREREG.md 在第一次真实实验前提交，之后只能在底部追加偏差，
并写明做决定时看过哪些数据。

**Q26. 配对设计的单位是什么？**
独立的源 run。同一个源 run 进化出的 harness 分别以 Full SIM 和 Harness Transfer 继续，配对差值消掉了源 harness 质量的差异。

**Q27. n = 3 能得出什么结论？**
只能描述。精确符号秩检验在 n = 3 时最小可能的双侧 p 是 2/8 = 0.25，永远达不到 0.05；t 检验自由度 2，置信区间很宽。
40 道 held-out 题里一题就是 2.5 分。所以报告里写的是「方向和量级」，不是显著性。

**Q28. 为什么用 sub-split？**
钱不够跑全量。sub-split 是冻结 split 的种子均匀抽样（seed 20261119，文件 sha256 写在 PREREG）。
因为每道题独立求解、温度 0、固定 seed，所以已缓存的父 split 评测可以直接投影成 sub-split 评测，不用重新花钱。

**Q29. 统计代码怎么验证的？**
用论文 Table 3 的每个 run 数据复现了论文报告的 t、p、CI、符号秩 p、d_z（tests/test_stats.py），
顺便发现论文附录说的「210 种符号组合」应为 2^10 = 1024。

### G. 工程

**Q30. 实验怎么做到可恢复？**
每轮的提议 manifest、screen/select 结果、评测都落盘并按内容哈希缓存；恢复时复用，不额外花调用（有测试）；
SIM 更新幂等；调度器重跑同一命令就从断点继续。

**Q31. 遇到过哪些真实 bug？**
（1）标准库 HTTP server listen backlog 默认 5，16 并发时连接被 reset，改 512 + 客户端重试；
（2）JSON 里转义长代码经常出错，改成 JSON manifest + 原文 `<file>` 块；
（3）403 被当成做错题（见 Q19）；
（4）相对路径的实验根目录在项目仓库里被 .gitignore 忽略，`git add` 失败——仓库和 worktree 路径一律转绝对路径，加了回归测试；
（5）`.gitignore` 里的 `runs/` 把 `results/runs/` 也忽略了，导致导出的原始文件一直没进仓库，改成 `/runs/`。

**Q32. 测试策略？**
离线 CI 全用 fake 模型（确定性响应器）+ 子进程后端，覆盖网关、SIM、引擎、调度器、报告；Docker/gVisor 测试单独标记；
`make flaky` 跑 10 遍查不稳定；mypy strict + ruff。

### H. 结果（数字全部出自 `results/summary.json`，括号里是 key）

**Q33. 主问题（RQ1）的结论是什么？**
3 对配对：Full SIM − Harness Transfer 在 BIRD held-out（40 题）上分别是 0.0、−10.0、+5.0 分，均值 −1.67，
95% CI [−20.6, +17.3]，t(2) = −0.38，p = 0.74，精确符号秩 p = 1.0（`paired`）。
正确的说法是「这个规模下没有检测到效果，也没有能力检测到」——不是「SIM 没用」。

**Q34. 实验里最有意思的发现？**
（1）3 个 Spider 源 run 独立地找到了同一个修改：先执行 SQL，结果为空就让模型修；evolve 83.3 → 90/93.3/90，
但 held-out 三个都从 80.0 掉到 77.5——过拟合了 30 道 evolve 题（`source`）。
（2）把 Spider 上进化出的 harness 拿到 BIRD，起点全部低于原始种子 harness（55.0/62.5/65.0 vs 67.5）：负迁移。
（3）同一个 harness 在同样 40 道题上评测两次，分数差到 5 分（`paired_raced`）——噪声和效果一样大。
（4）support-ops 的目标 held-out 一开始就是 100 分，四个条件都区分不出来（`conditions.supportops`）。

**Q35. 一共花了多少钱？怎么保证没超？**
修订后的全部实验 ¥5.384（上限 ¥9.50），5,184 次调用；policy ¥4.557、proposer ¥0.614、selector ¥0.213；
58% 的 policy prompt token 命中了供应商缓存（`results/budget/spend_ledger.json`）。
保证不超：网关的预留-结算机制（Q17）+ 调度器的 money gate（Q18）。

**Q36. 如果有更多预算，你会怎么改实验？**
按 PREREG 原计划跑 10 对、N=4、k=3、全量 split；每道题多次 rollout 降低噪声带；换更强的 proposer；
做一个更难的 support-ops 目标 split；把跨模型族（xm-*，已在计划里、离线用模拟模型测过）真跑起来。
