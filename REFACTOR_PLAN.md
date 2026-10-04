# 代码重构计划：三层分离架构 ✅ 已完成

## 目标
将 `english_coach/agent.py` (1655行) 重构为清晰的三层架构：
- `config.py` - 配置层
- `schemas.py` - 数据模型层  
- `agents.py` - Agent 定义层
- `workflow.py` - 业务逻辑层
- `utils.py` - 工具函数层

## 完成状态

✅ `config.py` - 集中管理所有模型配置和常量
✅ `schemas.py` - 提取所有 Pydantic 数据模型
✅ `agents.py` - 提取三个 Agent 定义（引用 config 和 schemas）
✅ `utils.py` - 提取所有辅助函数（文件名处理、日期解析、评分计算等）
✅ `workflow.py` - 保留 workflow 编排逻辑和 node 函数
✅ 更新 `agent.py` - 保持向后兼容，从新模块导入所有内容
✅ 验证导入 - 所有导入测试通过

## 重构完成时间

2026-10-02

所有功能已验证，详见 `REFACTORING_COMPLETE.md`

## 文件职责划分

### config.py ✅
- 模型选择（DEFAULT_MODEL, CLASSIFIER_MODEL 等）
- 生成配置（CLASSIFIER_CONFIG, EXTRACTOR_CONFIG 等）
- 重试参数（PROCESS_INPUT_MAX_ATTEMPTS, RETRY_INITIAL_DELAY）
- MIME 类型映射
- 默认反馈语言

### schemas.py ✅
- 所有 Pydantic BaseModel 类
- 类型别名（FeedbackLanguage, InputRoute 等）
- 数据验证逻辑

### agents.py ✅
- classify_input_image
- extractor  
- grammar_training_extractor

### utils.py ⬜
需要提取的函数：
- `_content_text()` - Content 转文本
- `_node_input_text()` - 节点输入转文本
- `_has_ascii_alias()` - ASCII 别名检测
- `_feedback_language_from_input()` - 反馈语言推断
- `_blob_bytes()` - Blob 数据转换
- `_mime_for_inline_image()` - MIME 类型推断
- `_safe_upload_filename()` - 安全文件名生成
- `_unique_upload_path()` - 唯一路径生成
- `_safe_filename_segment()` - 文件名段处理
- `_canonical_input_stem()` - 标准输入文件名
- `_known_name()` - 已知名称判断
- `_student_hint_from_filename()` - 从文件名推断学生名
- `_resolve_student_name()` - 解析学生名
- `_normalize_submission_date()` - 标准化提交日期
- `_default_submission_date()` - 默认提交日期
- `_resolve_submission_date()` - 解析提交日期
- `_unique_path()` - 生成唯一路径
- `_model_from_output()` - 从输出转换模型
- `_calculate_overall_score()` - 计算总分
- `_score_from_evidence()` - 从证据计算分数
- `_image_content()` - 生成图像 Content
- `_issue_key()` - 问题关键词
- `_is_same_issue_text()` - 判断相同问题
- `_writing_issue_fix()` - 写作问题修复
- `_writing_learning_needs()` - 写作学习需求
- `_grammar_learning_needs()` - 语法学习需求
- `_short_failure_reason()` - 简短失败原因
- `_task_for_attempt()` - 重试任务文本
- `_yaml_string()` - YAML 字符串转换
- `_markdown_cell()` - Markdown 单元格转义
- `_i18n_report()` - 国际化报告文本
- `_render_profile_markdown()` - 渲染个人档案 Markdown
- `_coerce_results()` - 强制转换结果
- `_result_with_filename()` - 带文件名的结果
- `_rename_processed_input()` - 重命名已处理输入

### workflow.py ⬜
保留的内容：
- `list_writing_inputs()` - 列出写作输入
- `pick_input_route()` - 选择输入路由
- `process_one_input()` - 处理单个输入 (@node)
- `orchestrate()` - 编排 (@node)
- `rename_processed_inputs()` - 重命名已处理输入
- `build_student_profiles()` - 构建学生档案
- `write_report()` - 写报告
- `root_agent` - Workflow 定义

### agent.py（保持向后兼容）⬜
```python
"""
Backward compatibility: re-export all components.
"""
from .agents import (
    classify_input_image,
    extractor,
    grammar_training_extractor,
)
from .config import *
from .schemas import *
from .utils import *
from .workflow import root_agent

__all__ = [
    "classify_input_image",
    "extractor", 
    "grammar_training_extractor",
    "root_agent",
    # ... 其他导出
]
```

## 优势

### 1. 配置灵活
```python
# 切换模型：只需修改 config.py
DEFAULT_MODEL = "gemini-pro-latest"  # 升级到更强模型
CLASSIFIER_MODEL = "gemini-flash-latest"  # 分类用快速模型
```

### 2. Agent 复用
```python
# 在新的 workflow 中复用已有 Agent
from english_coach.agents import classify_input_image, extractor

new_workflow = Workflow(
    name="advanced_coach",
    edges=[
        ("START", classify_input_image, ...),
    ]
)
```

### 3. 易于测试
```python
# 独立测试 Agent
from english_coach.agents import classify_input_image
from english_coach.schemas import ImageCategory

# Mock 输入测试
result = await classify_input_image.run(test_input)
assert isinstance(result, ImageCategory)
```

### 4. 清晰维护
- 修改模型参数 → 只看 `config.py`
- 修改数据结构 → 只看 `schemas.py`  
- 修改 Agent 指令 → 只看 `agents.py`
- 修改业务流程 → 只看 `workflow.py`

## 实施步骤

1. ✅ 创建 config.py
2. ✅ 创建 schemas.py
3. ✅ 创建 agents.py
4. ⬜ 创建 utils.py（提取所有 `_` 开头的辅助函数）
5. ⬜ 创建 workflow.py（提取 workflow 相关代码）
6. ⬜ 更新 agent.py 为兼容层
7. ⬜ 运行测试确保功能正常
8. ⬜ 更新文档

## 兼容性保证

重构后，现有代码无需修改：
```python
# 旧代码依然工作
from english_coach.agent import root_agent, classify_input_image
```

因为 `agent.py` 会重新导出所有组件。
