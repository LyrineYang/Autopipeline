# 产品与 benchmark 工作目录

更新于 2026-11-16。AutoPipeline 有两个日常工作树。

| 分支 | 职责 | 目录 |
| --- | --- | --- |
| `main` | CLI、通用优化与验证、预算、案例、报告、导出 | `/Users/nianzhen/Desktop/autopipeline` |
| `research/level5` | Level 5 的语音、文档、图像题目，固定基线和实验统计 | `/Users/nianzhen/.codex/worktrees/4f0c/autokernel` |

`research/level5` 合并了原 `codex/level5` 和 `codex/apimetric`。合并提交的
两个父提交分别保存原实验代码。产品预算约束运行，benchmark 记录每轮结果和用量。
旧 benchmark 的运行目录和锁文件归档在
`workspace/worktree-retirement-20261116/`。

产品目录原名为 Desktop/autokernel。现有 CPU 验收环境位于
`workspace/product-test-env/`。迁移验收索引位于
`workspace/path-migration-20260917/acceptance.json`。

## 2026-11-16 回收记录

- `c1c0`、`c343` 曾检出 `c259b8a`，提交已在产品 main 历史中。
- `5ea4` 曾检出 `04107f4`，这份 KernelBench 流程统计原型可以通过
  `refs/codex/snapshots/a141ae44d777dff63ae8e7ab9c40910dd3bec847` 恢复。
- `112d` 的实验代码通过合并提交保留，实验产物已归档。
- 本地发布准备分支已回收，GitHub 上的分支仍是仓库默认入口。

## 2026-09-14 的历史回收记录

最初 8 个旧目录均无未提交源码或独有提交，关联任务未运行。回收使用
`git worktree remove`，保留全部 Git 提交与任务历史，不删除主线设计文档。
目录前缀为 `/Users/nianzhen/.codex/worktrees/<编号>/autokernel`。

| 已回收编号 | 原提交 | 用途 |
| --- | --- | --- |
| `3fc8` | `80b34880d79fb2f62cf2e5d7b2d4ad3b6e9d6ae5` | 已合并 tree 开发线 |
| `139c`、`55fb`、`6046`、`cf83` | `c259b8a1142061fea21dca5a4f7eaf15fdff30cc` | 旧发布准备快照 |
| `301a`、`d831` | `1a1163992195b368a2004287c18e2a47dfd9b863` | 旧 main 快照 |
| `5b15` | `78435821cc3d5756ba6ee1785c397f6d8fa8c90d` | 上游基点快照 |

整理期间，`Standup summary` 自动化另建了 `7c06`（提交 `2b8c9cb0983c8e6c17223326e7d2666f62594e3a`）。
任务结束后，该干净目录也已回收，共回收 9 个目录。定时任务配置保持不变，
未来运行可能再次生成 worktree；当时保留的三个目录是上表中的长期工作目录。

需要重现旧检出时，从产品目录执行 Git 命令。下例使用表中的提交恢复独立 worktree。

```sh
git worktree add --detach /Users/nianzhen/.codex/worktrees/3fc8/autokernel 80b34880d79fb2f62cf2e5d7b2d4ad3b6e9d6ae5
```

`codex/tree` 与 `codex/open-source-release-prep` 的冗余本地分支名已清理；
其提交都保留在 main 历史中，远端分支未改动。

## 仓库与网站

- `origin`：`https://github.com/LyrineYang/Autopipeline.git`，独立产品仓库。
- `autokernel-base`：`https://github.com/RightNow-AI/autokernel.git`，上游比较来源。
- `main` 不跟踪上游 main，默认推送目标为自己的 origin；本次未执行网络推送。
- 网站已移至 `/Users/nianzhen/Desktop/autopipeline-site`；源码、锁文件和部署配置
  保留，npm 缓存和 dist 已清理。它不参与库安装、打包或 CI。

本地操作记录保存在 `workspace/worktree-retirement-audit.json` 和
`workspace/site-relocation.json`，不随 Git 发布。

```sh
git worktree list
git branch -vv
git log --graph --oneline --decorate --all
```
