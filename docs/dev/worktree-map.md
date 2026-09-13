# 产品主线与 benchmark 工作目录

更新：2026-09-14。worktree 是同一 Git 仓库的并列检出目录，不是产品运行架构。

## 活跃开发线

| 开发线 | 职责 | 目录 |
| --- | --- | --- |
| `main` | AutoPipeline 产品：CLI、通用优化/验证、预算、案例、报告与导出 | `/Users/nianzhen/Desktop/autokernel` |
| `codex/level5` | 独立 benchmark：题目、组合工作负载、基线与评分 | `/Users/nianzhen/.codex/worktrees/4f0c/autokernel` |
| `codex/apimetric` | 独立 benchmark：调用量、耗时、成本与实验统计 | `/Users/nianzhen/.codex/worktrees/112d/autokernel` |

Level 5 与 apimetric 都保持独立，不合入产品 main。两者目前有重叠的 Level 5
历史样例；该重叠留给 benchmark 开发线整理，不通过产品合并去隐式选择题目版本。
产品自己的预算账本用于约束运行，不能替代 benchmark 的实验统计。

## 产品整合

产品 main 包含原 main 的 tree runtime、已合并的 `codex/tree`、release-preparation
的 CLI 基础，以及本次提交的 pipeline 产品化、验证修正、案例和文档。
整合使用快进，不重写已有提交。`main` 不再跟踪 `autokernel-base/main`；
默认推送目标配置为自己的 `origin`，尚未执行网络推送或创建远端 main。

- `origin`：`https://github.com/LyrineYang/Autopipeline.git`，产品仓库。
- `autokernel-base`：`https://github.com/RightNow-AI/autokernel.git`，上游来源与比较。
- `codex/open-source-release-prep` 保留为整合来源/历史分支，不再作为产品主入口。
- `codex/tree` 保留在旧工作目录中，其历史已合并。
- `codex/l5merge` 仅指向共同基点，没有独有提交或 worktree，作为冗余本地分支清理。

## 保留的历史 worktree

这些目录没有因此被删除；对应 Codex 任务可能仍引用它们。
编号位于 `/Users/nianzhen/.codex/worktrees/<编号>/autokernel`。

| 编号 | 检出内容 | 整理前状态 |
| --- | --- | --- |
| `3fc8` | `codex/tree` / `80b3488` | 干净；已合并 |
| `139c`、`55fb`、`6046`、`cf83` | detached / `c259b8a` | 干净；旧发布准备快照 |
| `301a`、`d831` | detached / `1a11639` | 干净；旧 main 快照 |
| `5b15` | detached / `7843582` | 干净；上游基点快照 |

共保留 11 个 worktree。Level 5 和 apimetric 原有的未提交工作不变；
主工作目录的 `site/` 是已有未提交网站工作，本次保留但未纳入产品主线提交。
产品 CI 仅运行库的检查，不依赖这份未提交网站。

后续回收旧目录之前，先确认对应任务不再使用并再次检查未提交内容。
当前主线已经明确，不需要为了清理目录中断产品开发。

```sh
git worktree list
git branch -vv
git log --graph --oneline --decorate --all
```
