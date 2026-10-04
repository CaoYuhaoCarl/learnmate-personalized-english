# English Coach 快速参考

## 🚀 5 分钟上手

### 导入方式

```python
# 方式 1：向后兼容（适合现有代码）
from english_coach.agent import root_agent, classify_input_image

# 方式 2：模块化导入（推荐新代码）
from english_coach import config, schemas, agents, utils, workflow
```

### 运行 Workflow

```python
from english_coach.workflow import root_agent

# 运行完整流程
result = await root_agent.run(user_input)
```

## 📋 常用操作

### 1. 修改模型配置

**文件**：`english_coach/config.py`

```python
# 全局切换模型
DEFAULT_MODEL = "gemini-pro-latest"

# 或分 Agent 配置
CLASSIFIER_MODEL = "gemini-flash-latest"  # 快速
EXTRACTOR_MODEL = "gemini-pro-latest"     # 精细
```

### 2. 调整重试次数

**文件**：`english_coach/config.py`

```python
PROCESS_INPUT_MAX_ATTEMPTS = 5  # 从 3 改为 5
RETRY_INITIAL_DELAY = 3         # 从 2 秒改为 3 秒
```

### 3. 修改 Agent 指令

**文件**：`english_coach/agents.py`

```python
classify_input_image = Agent(
    name="classify_input_image",
    model=config.CLASSIFIER_MODEL or config.DEFAULT_MODEL,
    instruction=(
        "你的新指令..."  # 修改这里
    ),
    output_schema=ImageCategory,
    generate_content_config=config.CLASSIFIER_CONFIG,
)
```

### 4. 添加新字段

**文件**：`english_coach/schemas.py`

```python
class WritingEvidence(BaseModel):
    student_name: str
    # ... 现有字段 ...
    new_field: str = ""  # 添加新字段
```

### 5. 添加新工具函数

**文件**：`english_coach/utils.py`

```python
def my_new_function(input_data: str) -> str:
    """新功能的描述."""
    # 实现
    return result
```

### 6. 修改评分逻辑

**文件**：`english_coach/utils.py`  
**函数**：`score_from_evidence()`

```python
def score_from_evidence(evidence: WritingEvidence) -> DimensionScores:
    # 修改评分算法
    content = ...  # 新的内容分计算
    structure = ...  # 新的结构分计算
    ...
```

### 7. 添加新语言支持

**文件 1**：`english_coach/config.py`

```python
FeedbackLanguage = Literal["zh-Hans", "en", "ja", "ko", "es"]  # 添加 "es"
```

**文件 2**：`english_coach/utils.py`

```python
def feedback_language_from_input(node_input: object) -> FeedbackLanguage:
    # 添加检测逻辑
    if "español" in text or "spanish" in text:
        return "es"
    ...

def i18n_report(key: str, lang: FeedbackLanguage) -> str:
    translations = {
        "title": {
            "es": "Perfil de Aprendizaje",  # 添加西班牙语翻译
            ...
        }
    }
```

## 🔍 查找代码位置

| 需要修改 | 文件位置 | 函数/类 |
|---------|---------|--------|
| 模型名称 | `config.py` | `DEFAULT_MODEL` |
| 生成参数 | `config.py` | `*_CONFIG` |
| 数据结构 | `schemas.py` | `*Evidence`, `*Profile` |
| Agent 指令 | `agents.py` | `classify_input_image`, `extractor`, ... |
| 评分算法 | `utils.py` | `score_from_evidence()` |
| 文件名处理 | `utils.py` | `safe_filename_segment()`, ... |
| 国际化文本 | `utils.py` | `i18n_report()` |
| Workflow 流程 | `workflow.py` | `list_writing_inputs()`, `orchestrate()`, ... |

## 🐛 常见问题

### Q: 修改配置后不生效？

**A**: 检查是否重新导入了模块：

```python
# 错误：修改 config.py 后，这个不会更新
from english_coach.config import DEFAULT_MODEL

# 正确：每次导入模块
from english_coach import config
print(config.DEFAULT_MODEL)  # 总是最新值
```

### Q: 如何添加新的 Agent？

**A**: 三步走：

1. 在 `schemas.py` 定义输出 schema
2. 在 `agents.py` 定义 Agent
3. 在 `workflow.py` 中使用

```python
# 1. schemas.py
class NewEvidence(BaseModel):
    field1: str
    field2: int

# 2. agents.py
new_agent = Agent(
    name="new_agent",
    model=config.DEFAULT_MODEL,
    instruction="...",
    output_schema=NewEvidence,
)

# 3. workflow.py
evidence = await _run_extractor(
    ctx,
    agent=new_agent,
    schema=NewEvidence,
    ...
)
```

### Q: 如何测试单个函数？

**A**: 直接导入测试：

```python
from english_coach.utils import score_from_evidence
from english_coach.schemas import WritingEvidence

# 构造测试数据
evidence = WritingEvidence(
    student_name="Test",
    required_points_total=3,
    required_points_covered=3,
    grammar_errors=[],
    spelling_errors=[],
    has_clear_structure=True,
    has_conclusion=True,
    handwriting_legibility="excellent",
    strengths=["Good"],
    improvements=[],
    prompt_summary="Test",
    transcription="Test",
)

# 测试
score = score_from_evidence(evidence)
print(score)  # DimensionScores(content=5, structure=5, language=5.0, handwriting=5)
```

### Q: 如何回滚重构？

**A**: 恢复备份文件：

```bash
cd english_coach/
mv agent.py agent_refactored.py
mv agent.py.backup agent.py
```

## 📊 性能优化建议

### 1. 减少 token 消耗

```python
# config.py
EXTRACTOR_CONFIG = types.GenerateContentConfig(
    max_output_tokens=8192,  # 从 16384 降低
)
```

### 2. 调整并行度

```python
# workflow.py - orchestrate()
# 当前：无限制并行
tasks = [ctx.run_node(...) for item in inputs]

# 优化：限制并发数
from asyncio import Semaphore
sem = Semaphore(5)  # 最多 5 个并发
async def process_with_limit(item):
    async with sem:
        return await ctx.run_node(process_one_input, node_input=item)
tasks = [process_with_limit(item) for item in inputs]
```

### 3. 跳过不必要的步骤

```python
# workflow.py - write_report()
# 如果不需要 PDF
if not ENABLE_PDF_EXPORT:
    pdf_path = None  # 跳过 export_report_pdf()
```

## 🎯 最佳实践

### ✅ DO

```python
# 使用类型注解
def my_function(name: str, age: int) -> dict[str, str]:
    ...

# 使用明确的导入
from english_coach.utils import score_from_evidence

# 使用配置而非硬编码
model = config.DEFAULT_MODEL  # 不是 "gemini-flash-latest"

# 添加文档字符串
def my_function(x: int) -> int:
    """计算 x 的平方.
    
    Args:
        x: 输入整数
        
    Returns:
        x 的平方
    """
    return x * x
```

### ❌ DON'T

```python
# 避免通配符导入
from english_coach.config import *

# 避免导入私有函数
from english_coach.utils import _content_text  # 以 _ 开头

# 避免硬编码配置
agent = Agent(model="gemini-flash-latest")  # 应该用 config.DEFAULT_MODEL

# 避免魔法数字
score = x * 0.5  # 应该定义为常量 PENALTY_FACTOR = 0.5
```

## 📚 延伸阅读

- [ARCHITECTURE.md](ARCHITECTURE.md) - 完整架构文档
- [REFACTORING_COMPLETE.md](REFACTORING_COMPLETE.md) - 重构完成报告
- [REFACTOR_PLAN.md](REFACTOR_PLAN.md) - 重构计划

## 🆘 获取帮助

遇到问题？检查顺序：

1. 查看 [ARCHITECTURE.md](ARCHITECTURE.md) 了解设计
2. 查看源代码注释和文档字符串
3. 运行测试验证行为
4. 查看 Git 历史了解变更原因

---

**快速参考版本**：1.0  
**最后更新**：2026-10-02
