# English Coach 架构文档

## 🏗️ 模块化架构

```
english_coach/
├── config.py          # 配置层：模型选择、生成参数、常量
├── schemas.py         # 数据层：Pydantic 模型定义
├── agents.py          # Agent 层：AI Agent 实例
├── utils.py           # 工具层：辅助函数
├── workflow.py        # 业务层：Workflow 编排
└── agent.py           # 兼容层：向后兼容的导出
```

## 📊 数据流图

```
用户输入 (images/text)
    ↓
list_writing_inputs() → 发现输入文件
    ↓
orchestrate() → 并行分发
    ↓
    ├─→ process_one_input() → classify_input_image
    │       ↓
    │       ├─→ [route: writing] → extractor → WritingEvidence
    │       ├─→ [route: grammar] → grammar_training_extractor → GrammarTrainingEvidence
    │       └─→ [route: unsupported] → 跳过
    ↓
rename_processed_inputs() → 标准化文件名
    ↓
build_student_profiles() → 聚合为学生档案
    ↓
write_report() → 生成 Markdown + JSON + PDF
    ↓
输出 (reports/ + training_inputs/ + pdf_exports/)
```

## 🔧 依赖关系

### 教师 Web 入口

`web_app.py` 提供首页、鉴权、提交与报告接口，并保留 ADK 开发界面。
`submissions.py` 校验上传、管理单进程后台任务，将提交状态与结果写入独立目录。
`submission_runner.py` 为每次提交创建显式输入的 ADK Workflow，复用分类和提取，保留原始证据并生成教师草稿。
它不调用本地目录扫描入口，也不自动打开 WPS；未知学生不会自动合并到唯一已知学生。
Web 上传的原图保留原文件，通过稳定输入 ID 关联校对项；既有 ADK Workflow 仍保持原来的重命名和直接报告流程。
`review_models.py` 校验教师修订，确定性计算系统建议和最终评分，并从同一份错题列表派生练习重点。
`reviews.py` 管理草稿版本、确认快照和独立导出任务；保存与确认在事件循环内进行版本校验和原子写入，耗时导出使用快照副本。
每次导出结束重新读取最新状态，只更新对应版本；失败重试累积成功报告，不覆盖教师新草稿。
教师确认模式的报告将编辑内容作为纯文本渲染，原始 JSON 保留内容，并在 PDF 标明版本与时间。
`static/teacher.*` 和 `static/review.*` 提供无需构建的教师页面与原图对照编辑器。
任务状态与报告持久化独立于 ADK 的临时会话；中断任务标记中断，由老师决定是否重新提交。

```
workflow.py
    ├─→ agents.py
    │       ├─→ schemas.py
    │       └─→ config.py
    ├─→ utils.py
    │       ├─→ schemas.py
    │       └─→ config.py
    └─→ constants.py

agent.py (兼容层)
    ├─→ config.py
    ├─→ schemas.py
    ├─→ agents.py
    ├─→ utils.py (可选)
    └─→ workflow.py
```

## 📦 模块职责

### `config.py` - 配置中心

**职责**：集中管理所有可配置项

**内容**：
- `DEFAULT_MODEL`: 全局默认模型
- `CLASSIFIER_MODEL`: 分类模型（可覆盖）
- `EXTRACTOR_MODEL`: 提取模型（可覆盖）
- `GRAMMAR_MODEL`: 语法模型（可覆盖）
- `CLASSIFIER_CONFIG`: 分类生成配置
- `EXTRACTOR_CONFIG`: 提取生成配置
- `GRAMMAR_EXTRACTOR_CONFIG`: 语法生成配置
- `PROCESS_INPUT_MAX_ATTEMPTS`: 最大重试次数
- `RETRY_INITIAL_DELAY`: 重试初始延迟
- `MIME_BY_SUFFIX`: 文件类型映射
- `DEFAULT_FEEDBACK_LANGUAGE`: 默认反馈语言

**修改场景**：
- 升级/切换模型
- 调整 token 限制
- 修改重试策略
- 添加支持的文件类型

---

### `schemas.py` - 数据模型

**职责**：定义所有数据结构

**核心模型**：
- `ImageCategory`: 图像分类结果
- `WritingEvidence`: 写作证据提取
- `GrammarTrainingEvidence`: 语法训练提取
- `EnglishCoachFeedback`: 教师反馈
- `LearningNeed`: 学习需求
- `StudentLearningProfile`: 学生学习档案
- `InputProcessingResult`: 处理结果

**修改场景**：
- 添加新字段
- 修改验证规则
- 扩展数据结构

---

### `agents.py` - Agent 定义

**职责**：定义 AI Agent 实例

**Agents**：
- `classify_input_image`: 图像分类器
  - 输入：图像 + 文件名
  - 输出：`ImageCategory`
  - 用途：路由到对应的提取器

- `extractor`: 写作证据提取器
  - 输入：写作作业图像
  - 输出：`WritingEvidence`
  - 用途：提取评分证据

- `grammar_training_extractor`: 语法训练提取器
  - 输入：语法练习图像
  - 输出：`GrammarTrainingEvidence`
  - 用途：提取错题

**修改场景**：
- 调整 Agent 指令
- 修改输出 schema
- 添加新的 Agent

---

### `utils.py` - 工具函数

**职责**：提供可复用的辅助函数

**功能分类**：

1. **文本处理**
   - `_content_text()`: 提取 Content 文本
   - `_node_input_text()`: 节点输入转文本

2. **语言检测**
   - `feedback_language_from_input()`: 检测反馈语言

3. **文件处理**
   - `safe_filename_segment()`: 安全文件名
   - `canonical_input_stem()`: 标准文件名
   - `unique_path()`: 唯一路径生成

4. **学生信息解析**
   - `resolve_student_name()`: 解析学生名
   - `resolve_submission_date()`: 解析提交日期

5. **评分计算**
   - `score_from_evidence()`: 从证据计算分数
   - `calculate_overall_score()`: 计算总分

6. **学习需求生成**
   - `writing_learning_needs()`: 写作学习需求
   - `grammar_learning_needs()`: 语法学习需求

7. **报告渲染**
   - `i18n_report()`: 国际化文本
   - `markdown_cell()`: Markdown 转义

**修改场景**：
- 优化算法
- 添加新工具函数
- 修复 bug

---

### `workflow.py` - 业务编排

**职责**：定义 Workflow 流程

**核心函数**：

1. **输入发现**
   - `list_writing_inputs()`: 列出待处理图像

2. **路由**
   - `pick_input_route()`: 选择处理路由

3. **处理**
   - `process_one_input()`: 处理单个输入
   - `orchestrate()`: 并行编排

4. **后处理**
   - `rename_processed_inputs()`: 重命名文件
   - `build_student_profiles()`: 构建档案
   - `write_report()`: 写报告

5. **Workflow 定义**
   - `root_agent`: 主 Workflow

**修改场景**：
- 调整处理流程
- 添加新的 node
- 修改并行策略

---

### `agent.py` - 兼容层

**职责**：保持向后兼容

**功能**：重新导出所有公共接口

**修改场景**：
- 添加新的导出
- 维护兼容性

## 🎯 设计原则

### 1. 单一职责原则 (SRP)
每个模块只负责一件事：
- `config.py` → 配置
- `schemas.py` → 数据
- `agents.py` → Agent
- `utils.py` → 工具
- `workflow.py` → 流程

### 2. 依赖倒置原则 (DIP)
高层模块不依赖低层模块：
- `workflow.py` 依赖抽象的 Agent 接口
- Agent 配置通过 `config.py` 注入

### 3. 开闭原则 (OCP)
对扩展开放，对修改封闭：
- 添加新 Agent：只需修改 `agents.py`
- 修改评分逻辑：只需修改 `utils.py`
- 不影响其他模块

### 4. 接口隔离原则 (ISP)
客户端不应依赖它不需要的接口：
- 只需配置？导入 `config`
- 只需数据模型？导入 `schemas`
- 需要完整功能？导入 `agent`

## 🔄 典型使用场景

### 场景 1：调整模型配置

```python
# 1. 编辑 config.py
DEFAULT_MODEL = "gemini-pro-latest"

# 2. 无需修改其他文件
# 3. agents.py 自动使用新配置
```

### 场景 2：添加新 Agent

```python
# 1. 在 schemas.py 定义输出 schema
class NewEvidence(BaseModel):
    ...

# 2. 在 agents.py 定义 Agent
new_agent = Agent(
    name="new_agent",
    model=config.DEFAULT_MODEL,
    instruction="...",
    output_schema=NewEvidence,
)

# 3. 在 workflow.py 中使用
# 4. 在 agent.py 导出（可选）
```

### 场景 3：扩展评分逻辑

```python
# 1. 在 utils.py 添加新函数
def advanced_score_from_evidence(evidence):
    # 新的评分逻辑
    ...

# 2. 在 workflow.py 中调用
score = utils.advanced_score_from_evidence(evidence)

# 3. 无需修改 Agent 定义
```

### 场景 4：国际化新语言

```python
# 1. 在 config.py 添加语言
FeedbackLanguage = Literal["zh-Hans", "en", "ja", "ko", "fr"]

# 2. 在 utils.py 的 i18n_report() 添加翻译
translations = {
    "title": {
        "fr": "Profil d'apprentissage",
        ...
    }
}

# 3. 在 utils.py 的 feedback_language_from_input() 添加检测
if "français" in text:
    return "fr"
```

## 📈 性能考量

### 并行处理
- `orchestrate()` 使用 `asyncio.gather()` 并行处理多个图像
- 每个图像独立处理，互不影响

### 重试策略
- `process_one_input()` 配置了 `RetryConfig`
- 最多重试 3 次（可在 `config.py` 调整）
- 失败后降级为 skipped 而非中断整个流程

### 资源管理
- 图像数据读取后立即传递给 Agent
- 不在内存中缓存大量图像
- 使用流式处理

## 🧪 测试策略

### 单元测试
```python
# 测试 utils 函数
from english_coach.utils import score_from_evidence

def test_perfect_score():
    evidence = WritingEvidence(
        required_points_total=3,
        required_points_covered=3,
        ...
    )
    score = score_from_evidence(evidence)
    assert score.content == 5
```

### 集成测试
```python
# 测试 Agent
from english_coach.agents import classify_input_image

async def test_classifier():
    result = await classify_input_image.run(test_input)
    assert isinstance(result, ImageCategory)
```

### 端到端测试
```python
# 测试完整 workflow
from english_coach.workflow import root_agent

async def test_workflow():
    result = await root_agent.run(user_input)
    assert len(result) > 0
```

## 📝 维护指南

### 添加新功能
1. 确定功能属于哪一层
2. 在对应模块添加代码
3. 更新 `agent.py` 导出（如需要）
4. 添加测试
5. 更新文档

### 修复 Bug
1. 定位 bug 所在模块
2. 在该模块修复
3. 添加回归测试
4. 验证不影响其他模块

### 重构代码
1. 遵循单一职责原则
2. 保持模块间低耦合
3. 使用类型注解
4. 添加文档字符串

## 🎓 学习资源

- Google ADK 文档：https://google.github.io/adk-docs/
- Pydantic 文档：https://docs.pydantic.dev/
- Python 异步编程：https://docs.python.org/3/library/asyncio.html

---

**版本**：2.0 (重构版)  
**更新时间**：2026-10-02  
**维护者**：English Coach 团队
