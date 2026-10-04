"""
Backward compatibility layer: re-export all components.

This module maintains the original API while delegating to the refactored
architecture. Existing code importing from this module will continue to work.

New code should import directly from the specialized modules:
    - config: Model and generation configurations
    - schemas: Pydantic data models
    - agents: Agent instances
    - utils: Helper functions
    - workflow: Workflow nodes and root_agent
"""

# Re-export configuration
from .config import (
    BASE_GENERATION_CONFIG,
    CLASSIFIER_CONFIG,
    CLASSIFIER_MODEL,
    DEFAULT_FEEDBACK_LANGUAGE,
    DEFAULT_MODEL,
    EXTRACTOR_CONFIG,
    EXTRACTOR_MODEL,
    GRAMMAR_EXTRACTOR_CONFIG,
    GRAMMAR_MODEL,
    MIME_BY_SUFFIX,
    PROCESS_INPUT_MAX_ATTEMPTS,
    RETRY_INITIAL_DELAY,
    SUFFIX_BY_MIME,
    FeedbackLanguage,
)

# Re-export schemas
from .schemas import (
    DimensionScores,
    EnglishCoachFeedback,
    GrammarTrainingEvidence,
    GrammarTrainingMistake,
    ImageCategory,
    InputProcessingResult,
    InputRoute,
    LearningNeed,
    LearningSource,
    StudentLearningProfile,
    WritingEvidence,
    WritingIssueFix,
    WritingTrainingFocus,
    WritingTrainingSkill,
)

# Re-export agents
from .agents import (
    classify_input_image,
    extractor,
    grammar_training_extractor,
)

# Re-export workflow components
from .workflow import (
    build_student_profiles,
    list_writing_inputs,
    orchestrate,
    pick_input_route,
    process_one_input,
    rename_processed_inputs,
    root_agent,
    write_report,
    InputProcessingError,
)

# Re-export commonly used utilities (optional - uncomment if needed)
# from .utils import (
#     feedback_language_from_input,
#     safe_filename_segment,
#     resolve_student_name,
#     score_from_evidence,
#     calculate_overall_score,
# )


__all__ = [
    # Config
    "BASE_GENERATION_CONFIG",
    "CLASSIFIER_CONFIG",
    "CLASSIFIER_MODEL",
    "DEFAULT_FEEDBACK_LANGUAGE",
    "DEFAULT_MODEL",
    "EXTRACTOR_CONFIG",
    "EXTRACTOR_MODEL",
    "GRAMMAR_EXTRACTOR_CONFIG",
    "GRAMMAR_MODEL",
    "MIME_BY_SUFFIX",
    "PROCESS_INPUT_MAX_ATTEMPTS",
    "RETRY_INITIAL_DELAY",
    "SUFFIX_BY_MIME",
    "FeedbackLanguage",

    # Schemas
    "DimensionScores",
    "EnglishCoachFeedback",
    "GrammarTrainingEvidence",
    "GrammarTrainingMistake",
    "ImageCategory",
    "InputProcessingResult",
    "InputRoute",
    "LearningNeed",
    "LearningSource",
    "StudentLearningProfile",
    "WritingEvidence",
    "WritingIssueFix",
    "WritingTrainingFocus",
    "WritingTrainingSkill",

    # Agents
    "classify_input_image",
    "extractor",
    "grammar_training_extractor",

    # Workflow
    "build_student_profiles",
    "list_writing_inputs",
    "orchestrate",
    "pick_input_route",
    "process_one_input",
    "rename_processed_inputs",
    "root_agent",
    "write_report",
    "InputProcessingError",
]
