# 产品与 benchmark 工作目录

更新：2026-09-14。当前只保留 **3 个 worktree**；它们是并列检出目录。

| 分支 | 职责 | 目录 |
| --- | --- | --- |
| `main` | CLI、通用优化/验证、预算、案例、报告与导出 | `/Users/nianzhen/Desktop/autokernel` |
| `codex/level5` | 独立 benchmark：题目、组合工作负载、基线与评分 | `/Users/nianzhen/.codex/worktrees/4f0c/autokernel` |
| `codex/apimetric` | 独立 benchmark：调用量、耗时、成本与实验统计 | `/Users/nianzhen/.codex/worktrees/112d/autokernel` |

Level 5 和 apimetric 不合入产品 main；原有未提交工作保持独立。
产品预算账本用于约束运行，不能替代 benchmark 的实验统计。

## 历史目录已回收

8 个旧目录均无未提交源码或独有提交，关联任务未运行。回收使用
`git worktree remove`，保留全部 Git 提交与任务历史，不删除主线设计文档。
目录前缀为 `/Users/nianzhen/.codex/worktrees/<编号>/autokernel`。

| 已回收编号 | 原提交 | 用途 |
| --- | --- | --- |
| `3fc8` | `80b34880d79fb2f62cf2e5d7b2d4ad3b6e9d6ae5` | 已合并 tree 开发线 |
| `139c`、`55fb`、`6046`、`cf83` | `c259b8a1142061fea21dca5a4f7eaf15fdff30cc` | 旧发布准备快照 |
| `301a`、`d831` | `1a1163992195b368a2004287c18e2a47dfd9b863` | 旧 main 快照 |
| `5b15` | `78435821cc3d5756ba6ee1785c397f6d8fa8c90d` | 上游基点快照 |

旧任务的目录已不存在；继续产品工作应使用当前 main。如果确实需要重现某个
旧检出，可根据表中的完整提交恢复独立 worktree，例如：

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
