# 产品发布检查

本页只描述 AutoPipeline 库的发布要求。网站独立维护，不参与 Python 产品的
安装、CI 或发布验收。当前分支与本地目录见 [worktree-map.md](worktree-map.md)，
本轮整理结论见 [cleanup-review.md](cleanup-review.md)。

## 当前可交付范围

产品版本为 `1.4.0a3`，原生入口是 `autopipeline pipeline`。
安装 wheel 后可以在源码目录之外初始化、检查、评估和导出工作负载。
`tree`、`profile`、`extract` 等历史命令仍有源码检出依赖，不能据此宣称所有
兼容命令都已经独立打包。Level 5 和 apimetric 是独立 benchmark 开发线。

本地已有 149 项测试通过记录，以及安装包运行 residual-mlp、attention、
hf-local 三个真实 CPU 工作负载的记录。hf-local 使用本地初始化的小型 checkpoint
验证模型加载，不代表已验证下载的预训练模型质量或真实 agent 的优化收益。
最新整理构建的本地验收索引是 `workspace/releases/acceptance.json`；它及日志属于运行产物，
不随 Git 发布。发布时应单独保存对应版本、提交、包哈希和日志。

## 每次库发布的必要检查

1. 核对版本、变更说明、MIT 上游署名和包元数据。
2. 运行单元测试及 CPU 产品集成测试；保留正确性拒绝、不可变参考、失败退出、
   profiler 和导出复验的覆盖。
3. 构建 wheel，在源码目录之外安装；运行 README 的初始化、检查、评估和导出流程。
4. 确认上传目标是自己的 `origin`；读取远端实际状态后选择推送/PR 路径。
   本地合并成功不等于远端已更新或 CI 已通过。
5. 对待发布文件和历史做隐私检查；发布产物只包含产品需要的文件。

```sh
python -m pip install -e '.[cpu,models]'
python -m unittest discover -s tests
python -m autopipeline --help
python -m autopipeline doctor --no-gpu
python -m pip wheel . --no-deps -w workspace/releases
```

CI 配置包含 Python 单元检查、wheel 构建与源码目录外检查、CPU 产品和模型案例
集成测试。Dependabot 只监控 Python 与 GitHub Actions。远端 CI 结果应在推送后确认。

## 需要继续推进的验证

| 需要回答的问题 | 为什么影响产品 | 所需资源与执行路径 | 暂无资源时的推进方式 |
| --- | --- | --- | --- |
| CUDA 上是否可安装、编译并正确执行？ | 决定服务器首次使用是否成功 | NVIDIA Linux 服务器；记录驱动、CUDA、PyTorch，运行小案例并保存 profiler/复验产物 | 保持 CPU 安装与失败诊断覆盖，准备相同 CLI 入口 |
| 真实 agent 能否完成一次编辑、测量和采纳/回退？ | 决定优化闭环是否可用 | 已认证的 agent 后端与明确调用预算；可先在 CPU 小案例运行 | 完成协议、超时、预算与失败路径验证；无需等待 GPU |
| 代表性工作负载是否获得可重复收益？ | 决定优化是否值得使用 | 用户选定任务、输入分布和权重；GPU 上比较固定参考与最终候选 | 先确定工作负载契约与比较指标，不用合成分数代替收益 |

以上缺口限制的是 CUDA/优化收益的结论，不阻止继续交付和改进可安装的 alpha 产品。

## 公共包发布前的结构问题

wheel 仍暴露 `tree`、`pipeline`、`kernelbench`、`kernels`、`models` 等顶层包名。
公开 PyPI 分发前应迁移到 `autopipeline.*` 或作出明确的兼容策略，尤其要检查
`hf-kernels` 可选依赖的 `kernels` 包与本地包的重名风险。
本地安装验收不等于已验证与其他 Python 项目的共存能力。

网站上线、benchmark 论文实验和库版本发布分别安排；不将网站构建或完整 GPU
实验设为所有产品维护工作的前置条件。

## 本次文档发布准备（2026-09-15）

只读检查 origin 得到：默认分支仍是 `codex/open-source-release-prep`，提交
`c259b8a`，尚无远端 main。因此现在默认克隆不会得到本地 a3 产品。
README 暂以对应源码包或 wheel 作为入口，不能将远端旧分支当作新用户验收结果。

实际发布按这个顺序进行：

1. 推送经审查的本地 main 到 origin，完成远端 CI；本次文档准备不执行推送。
2. 将 GitHub 默认分支切换为 main，确认可见性和目标受众。
3. 在全新目录克隆默认分支，照 README 安装、评估、报告、导出并复验；确认拿到
   对应版本后，将 README 的“维护者提供源码包”入口换成明确的克隆/版本下载入口。
4. 发布对应提交的源码包、wheel、哈希和使用说明。版本发布不附带未验证的 GPU 加速承诺。
5. 有已认证服务后先在 CPU 上做一次真实 agent 运行，再安排目标 GPU 兼容性与收益验证。

本地文档命令检查与 CPU 执行记录保存在 workspace 中；远端 CI、仓库可见性修改
和对外发布仍需在实际发布操作中确认，不通过修改文档假定已经完成。
