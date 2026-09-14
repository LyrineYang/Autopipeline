# 产品整理结果

完成日期：2026-09-14。本次整理保留可运行能力和全部设计文档，优先回收历史
worktree、分离网站、合并重复入口，并重新验证安装包。

## 工作树与分支

当前只保留 `main`、`codex/level5`、`codex/apimetric` 三个 worktree。
最初 8 个历史目录已通过 `git worktree remove` 正式回收：139c、301a、3fc8、55fb、
5b15、6046、cf83、d831。回收前确认：

- 无未提交源码、无独有提交；只有 3fc8 带有可重建的 Python 字节码缓存。
- 关联任务未运行，未发现使用这些目录的进程。
- 原提交已包含在产品 main 中，任务历史不删除。

整理期间，已结束的 `Standup summary` 自动化新增 `7c06`，也完成相同检查后回收。
因此本次共回收 9 个目录；定时任务设置不变，未来运行可能再次生成检出目录。

`codex/tree` 与 `codex/open-source-release-prep` 两个冗余本地分支也已删除，
其提交仍在 main 中；远端分支未改动。目录、提交和恢复方法见
[worktree-map.md](worktree-map.md)。

Level 5、apimetric 的 HEAD、完整状态和 81 个改动文件的哈希与整合前一致。
它们仍是独立 benchmark，未合入产品，也未参与本次代码精简。

## 网站与磁盘

| 内容 | 整理结果 |
| --- | --- |
| 网站 | 从产品目录迁至 `/Users/nianzhen/Desktop/autopipeline-site`；25 个保留文件逐项校验，源码、锁文件、README 和部署配置保留 |
| 网站缓存 | 清理 .npm-cache 和 dist，网站从约 237 MiB 降至 1.3 MiB |
| Git 对象 | 从约 1.9 GiB 降至 743 MiB；保留全部 6,190 个原对象，缺失为零 |
| 历史工作树 | 回收原有 8 个及新增 1 个目录，约 15 MiB |
| CPU 验收环境 | 保留约 749 MiB，用于后续产品验证；不是发布包的一部分 |
| 实验产物 | 保留运行结果、profiler、日志和各版本 wheel，不以少量空间换取证据丢失 |

本次约释放 1.4 GiB。Git 采用保留不可达对象的压缩打包，先核对对象清单和 pack
校验，再用 `git prune-packed` 移除已有压缩副本的重复文件。没有设置对象过期时间，
没有执行 `git gc --prune=now` 或清除恢复历史；未知临时对象文件继续保留。

## 代码与实际行为

- `autopipeline/report.py` 统一参数解析、报告生成和输出。
  `scripts/tree_report.py` 保留为兼容入口；两者结果与退出码统一。
  输入目录缺少 search_state.json 时明确失败，不再生成空报告目录。
- `pipeline/smoke.py` 承接合成 agent、fixture 和 evaluator 模板。
  核心任务实现减少约 200 行混杂代码；正常产品导入不加载 smoke。
  旧的 `pipeline.task` / `pipeline` smoke 导入方式仍可用。
- 真实 CPU 案例继续作为产品能力验收；合成 smoke 只验证控制流程，不能代表
  模型正确性、GPU 性能或 agent 的真实优化收益。
- 新构建版本 `1.4.0a3`，避免覆盖此前 a2 的包与验收证据。

## 设计文档与兼容边界

原有 26 个跟踪 Markdown 路径全部保留；planner 设计、tree 历史共识、agent
研究经验、Symphony 分析、交互指标设计、program.md、agent 设计背景及运行契约
均逐项确认内容未改动。未迁移或删除设计文档。

维护索引区分现行用法与历史研究；发布说明、worktree 清单、adapter 示例和
profiler 能力说明已更新。稳定运行路径仍由 README、quickstart 和 agent contract
负责，不将旧研究稿作为当前功能清单。

以下保留是兼容边界，不将其误判为死代码：

- profile.py / model_profile.py 分别负责标准库兼容入口与旧模型 profiler。
- AutoKernel 源码工具链与 KernelBench 适配仍有调用者，保留运行入口。
- evaluator、验证后端、profiler、sensing 各有不同职责。
- 顶层 tree、pipeline、kernelbench、kernels、models 包名仍保留。命名空间迁移
  会改变独立 benchmark 的导入接口，应作为有迁移与兼容验收的 API 变更处理，
  而非通过瘦身直接删除或重命名。公开 PyPI 分发前仍需解决共存风险。

## 验证

- 149 项单元和集成测试通过，包含错误候选拒绝、受保护参考、预算/失败路径、
  报告兼容、延迟加载，以及真实 CPU 模型测试。
- 安装 a3 wheel 到源码目录外，核对导入确实来自安装包。
- residual-mlp、attention、hf-local 三个案例均完成初始化、检查、实际张量评估、
  profiler 产物、报告、导出与导出后的重新评估。
- hf-local 使用本地初始化的小型 checkpoint，验证离线加载和执行；没有进行
  下载模型的质量评估、真实 agent 调用或 CUDA 实验。
- 包内 Python 文件逐项匹配整理后的源码。设计文档、两个 benchmark 和全部原
  Git 对象分别进行保留校验。

本地证据不随 Git 发布：`workspace/cleanup-tests.log`、
`workspace/cleanup-installation.log`、`workspace/cleanup-verification.json`、
`workspace/git-repack-verification.json`、`workspace/worktree-retirement-audit.json`、
`workspace/site-relocation.json`。最新版本索引是 `workspace/releases/acceptance.json`。

当前整理不依赖服务器；下一步真实 agent 闭环可先用 CPU 和已认证服务验证。
CUDA 兼容性与代表性工作负载收益仍需要目标服务器，资源路径见
[发布检查](open_source_readiness.md)。
