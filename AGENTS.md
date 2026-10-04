# 本仓库项目规则

<!-- agents:begin -->
## 项目与栈

- LearnMate 是学生英语作业反馈应用，使用 Google ADK 2.2.0、FastAPI 和 Pydantic；Docker 运行 Python 3.12，PDF 导出依赖 Pandoc 与 WeasyPrint。
- 修改代理或编排时，先读 `ARCHITECTURE.md` 并核对代码：`english_coach/agent.py` 是兼容入口，模型配置、数据模型、代理、工具函数和编排分别在 `config.py`、`schemas.py`、`agents.py`、`utils.py`、`workflow.py`。

## 验证入口

- PDF 工具检查：在仓库根运行 `make check-tools`，目标来自 `Makefile`。
- 测试：`tests/` 使用 `unittest`，运行命令见 `README.md` 的 Test 小节；manifest/配置未定义测试入口，类型检查与 lint 也未配置。

## 高危操作（执行前确认授权）

- 对真实作业运行工作流会向外部模型发送图像和文件名，并重命名原图；覆盖或删除 `english_coach/` 下的 `input/`、`reports/`、`training_inputs/` 数据，以及清理各级 `.adk/` 会话状态，均需确认授权。
- `make pdf`（`Makefile`）可能覆盖同名 PDF；Fly 应用创建、部署、密钥或存储变更按 `docs/deploy-fly.md` 核对影响并确认授权。

## 个人数据与密钥

- 本项目处理学生姓名、提交日期、作业图像与转录、原答案与错题、评分、评语和学习画像；本地输入、报告、训练 JSON 与 ADK 会话均可能包含这些数据。
- 凭证配置位于本地 `.env` 系列或 Fly secrets：`GOOGLE_API_KEY`、`REPORTS_USERNAME`、`REPORTS_PASSWORD`；位置与变量用途见 `docs/deploy-fly.md` 和 `english_coach/web_app.py`。

## 生成物（不要手改）

- `english_coach/reports/` 的 Markdown/PDF 与 `english_coach/training_inputs/*.json` 由 `workflow.py`、`pdf_export.py` 生成；修改生成逻辑后重新生成。
<!-- agents:end -->

## 本项目补充（手写，skill 不修改）
