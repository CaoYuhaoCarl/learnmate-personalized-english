"""
Agent definitions for English Coach workflow.

Each agent is a specialized multimodal extractor that processes student work
images and returns structured output. All agents use the model and generation
configs defined in config.py.
"""

from google.adk import Agent

from . import config
from .schemas import (
    GrammarTrainingEvidence,
    ImageCategory,
    WritingEvidence,
)


# ============================================================================
# Image Classifier
# ============================================================================

classify_input_image = Agent(
    name="classify_input_image",
    model=config.CLASSIFIER_MODEL or config.DEFAULT_MODEL,
    instruction=(
        "You are routing a teacher's uploaded English-learning screenshot."
        " The user message contains a filename label followed by one image.\n\n"
        "Return category as exactly one of:\n"
        "- writing: a writing prompt plus a student's handwritten response.\n"
        "- grammar_training: grammar practice, corrected grammar exercises,"
        " fill-in grammar answers, or visible grammar mistakes to review.\n"
        "- unsupported: anything else, unreadable images, or non-English work.\n\n"
        "student_name: return the student's name if visible, otherwise"
        " \"unknown\". submission_date: return the date visibly written or"
        " printed on the image, normalized as YYYY-MM-DD when possible;"
        " otherwise return an empty string. confidence: 0 to 1. reason: one"
        " short explanation."
    ),
    output_schema=ImageCategory,
    output_key="image_category",
    generate_content_config=config.CLASSIFIER_CONFIG,
)


# ============================================================================
# Writing Evidence Extractor
# ============================================================================

extractor = Agent(
    name="extractor",
    model=config.EXTRACTOR_MODEL or config.DEFAULT_MODEL,
    instruction=(
        "You are an experienced writing teacher. The user message contains a"
        " filename label followed by a single image. The image shows, top to"
        " bottom, the printed writing prompt and the student's handwritten"
        " response.\n\n"
        "Read both. Extract stable grading evidence only. Do not assign"
        " scores.\n\n"
        "student_name: the student's name as written on the image, usually at"
        " the top of the page or in a header/label area. Return only the name"
        " itself; strip labels like \"Name:\" / \"姓名:\" / \"Student:\"."
        " If you cannot find a name, return \"unknown\".\n"
        "submission_date: the date visibly written or printed on the image,"
        " usually near the top/header. Return it normalized as YYYY-MM-DD when"
        " possible. If no date is visible, return an empty string.\n"
        "prompt_summary: one sentence describing what the writing task was meant"
        " to address.\n"
        "transcription: the student's handwritten response transcribed"
        " verbatim. Preserve their original words, line breaks, spelling, and"
        " grammar; do not silently correct mistakes. Use \\n for line breaks."
        " Do not include the printed prompt.\n"
        "model_essay: an English high-scoring rewritten sample based on the"
        " prompt and the student's transcription. Preserve the student's main"
        " ideas when relevant, correct grammar and spelling, add missing"
        " required points if needed, and keep it age-appropriate. Return only"
        " the model essay text; do not include a title, bullets, or"
        " explanation. Use \\n for paragraph breaks.\n"
        "required_points_total: count distinct required content points in the"
        " prompt.\n"
        "required_points_covered: count how many required points the response"
        " addresses, even if imperfectly.\n"
        "grammar_errors: list distinct grammar errors in the response. Use"
        " short quoted snippets.\n"
        "grammar_error_fixes: one item per grammar_errors entry. error must"
        " exactly repeat the matching grammar_errors snippet. suggested_fix"
        " must be the corrected wording only, not an instruction to review."
        " explanation should briefly explain the correction.\n"
        "spelling_errors: list distinct spelling errors. Use short snippets.\n"
        "spelling_error_fixes: one item per spelling_errors entry. error must"
        " exactly repeat the matching spelling_errors snippet. suggested_fix"
        " must be the correct spelling only, not an instruction to review."
        " explanation should briefly explain the correction.\n"
        "has_clear_structure: true if the response has logical order or useful"
        " transitions.\n"
        "has_conclusion: true if it has a closing thought.\n"
        "handwriting_legibility: choose exactly one of excellent, clear,"
        " readable, hard_to_read, illegible.\n"
        "The user message includes feedback_language as one of zh-Hans, en,"
        " ja, or ko. Write prompt_summary, strengths, improvements,"
        " grammar_error_fixes.explanation, spelling_error_fixes.explanation,"
        " writing_training_focuses.suggested_fix, and"
        " writing_training_focuses.explanation in that feedback_language.\n"
        "strengths: 1-3 short bullets.\n"
        "improvements: 1-3 actionable bullets for teacher feedback.\n"
        "writing_training_focuses: 0-3 broader writing skill needs for future"
        " personalized practice. Do not repeat any item already captured in"
        " grammar_errors or spelling_errors. Use skill_tag exactly as one of"
        " writing_structure, content_development, cohesion, or"
        " vocabulary_precision. Use writing_structure for organization,"
        " paragraphing, or conclusions; content_development for missing prompt"
        " points, weak reasons, or thin examples; cohesion for transitions and"
        " logical links; vocabulary_precision for word choice, collocation, or"
        " naturalness that is not a spelling error or single grammar-form"
        " correction. evidence should be a short quote or observation;"
        " suggested_fix should be the practice focus; explanation should briefly"
        " explain why it matters. If all improvements are only grammar or"
        " spelling repeats, return an empty list."
    ),
    output_schema=WritingEvidence,
    generate_content_config=config.EXTRACTOR_CONFIG,
)


# ============================================================================
# Grammar Training Extractor
# ============================================================================

grammar_training_extractor = Agent(
    name="grammar_training_extractor",
    model=config.GRAMMAR_MODEL or config.DEFAULT_MODEL,
    instruction=(
        "You are an English grammar teacher. The user message contains a"
        " filename label followed by one grammar-training screenshot.\n\n"
        "Extract only grammar mistakes or wrong answers that should become"
        " future personalized practice. Do not include correct answers with no"
        " error.\n\n"
        "student_name: the student's name if visible; otherwise \"unknown\".\n"
        "submission_date: the date visibly written or printed on the image,"
        " normalized as YYYY-MM-DD when possible; otherwise an empty string.\n"
        "mistakes: one item per distinct grammar error or wrong answer.\n"
        "skill_tag: short lowercase English label, such as tense,"
        " subject_verb_agreement, article, plural, preposition, word_order, or"
        " punctuation.\n"
        "original_answer: the student's wrong answer or mistaken text exactly"
        " as visible.\n"
        "correct_answer: the corrected answer.\n"
        "explanation: concise teacher explanation. The user message includes"
        " feedback_language as zh-Hans, en, ja, or ko; write explanations in"
        " that language."
    ),
    output_schema=GrammarTrainingEvidence,
    generate_content_config=config.GRAMMAR_EXTRACTOR_CONFIG,
)
