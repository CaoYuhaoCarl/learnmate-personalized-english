"""
Pydantic schemas for English Coach agent inputs and outputs.

These define the structured data models used throughout the workflow:
- Input classification and routing
- Evidence extraction from student work
- Learning profiles and feedback reports
"""

from typing import Literal

from pydantic import BaseModel
from pydantic import Field


# ============================================================================
# Type Aliases
# ============================================================================

FeedbackLanguage = Literal["zh-Hans", "en", "ja", "ko"]
InputRoute = Literal["writing", "grammar_training", "unsupported"]
LearningSource = Literal["writing", "grammar_training"]
WritingTrainingSkill = Literal[
    "writing_structure",
    "content_development",
    "cohesion",
    "vocabulary_precision",
]


# ============================================================================
# Image Classification
# ============================================================================

class ImageCategory(BaseModel):
    """Classification result for one input image."""
    category: InputRoute
    student_name: str = "unknown"
    submission_date: str = ""
    confidence: float = Field(ge=0, le=1)
    reason: str


# ============================================================================
# Writing Evidence Extraction
# ============================================================================

class DimensionScores(BaseModel):
    """Multi-dimensional scoring for writing submissions."""
    content: int
    structure: int
    language: float
    handwriting: int


class WritingIssueFix(BaseModel):
    """One grammar or spelling error with correction."""
    error: str
    suggested_fix: str
    explanation: str


class WritingTrainingFocus(BaseModel):
    """Broader writing skill need for personalized practice."""
    skill_tag: WritingTrainingSkill
    evidence: str
    suggested_fix: str
    explanation: str


class WritingEvidence(BaseModel):
    """Complete evidence extraction from one writing submission image."""
    student_name: str
    submission_date: str = ""
    prompt_summary: str
    transcription: str
    model_essay: str = ""
    required_points_covered: int = Field(ge=0)
    required_points_total: int = Field(ge=0)
    grammar_errors: list[str]
    grammar_error_fixes: list[WritingIssueFix] = Field(default_factory=list)
    spelling_errors: list[str]
    spelling_error_fixes: list[WritingIssueFix] = Field(default_factory=list)
    has_clear_structure: bool
    has_conclusion: bool
    handwriting_legibility: Literal[
        "excellent",
        "clear",
        "readable",
        "hard_to_read",
        "illegible",
    ]
    strengths: list[str]
    improvements: list[str]
    writing_training_focuses: list[WritingTrainingFocus] = Field(
        default_factory=list
    )


# ============================================================================
# Grammar Training Evidence Extraction
# ============================================================================

class GrammarTrainingMistake(BaseModel):
    """One grammar error from training exercises."""
    skill_tag: str
    original_answer: str
    correct_answer: str
    explanation: str


class GrammarTrainingEvidence(BaseModel):
    """Complete evidence extraction from one grammar training image."""
    student_name: str = "unknown"
    submission_date: str = ""
    mistakes: list[GrammarTrainingMistake] = Field(default_factory=list)


# ============================================================================
# Learning Profiles & Feedback
# ============================================================================

class EnglishCoachFeedback(BaseModel):
    """Teacher-facing feedback for one writing submission."""
    filename: str
    student_name: str
    feedback_language: FeedbackLanguage = "zh-Hans"
    prompt_summary: str
    transcription: str
    model_essay: str = Field(default="", exclude=True)
    overall_score: float
    dimensions: DimensionScores
    strengths: list[str]
    improvements: list[str]


class LearningNeed(BaseModel):
    """One personalized training need extracted from student work."""
    student_name: str
    source_type: LearningSource
    filename: str
    skill_tag: str
    evidence: str
    suggested_fix: str
    explanation: str


class InputProcessingResult(BaseModel):
    """Complete processing result for one input image."""
    filename: str
    source_path: str = ""
    category: InputRoute
    student_name: str
    submission_date: str = ""
    feedback_language: FeedbackLanguage = "zh-Hans"
    feedback: EnglishCoachFeedback | None = None
    grammar_training: GrammarTrainingEvidence | None = None
    learning_needs: list[LearningNeed] = Field(default_factory=list)
    skipped_reason: str | None = None


class StudentLearningProfile(BaseModel):
    """Aggregated learning profile for one student across all submissions."""
    student_name: str
    feedback_language: FeedbackLanguage = "zh-Hans"
    feedback_items: list[EnglishCoachFeedback] = Field(default_factory=list)
    grammar_trainings: list[GrammarTrainingEvidence] = Field(default_factory=list)
    learning_needs: list[LearningNeed] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
