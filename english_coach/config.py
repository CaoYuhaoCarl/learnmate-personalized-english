"""
Configuration for English Coach agents and workflow.

Centralizes all model settings, generation configs, and runtime parameters.
Modify here to switch models, adjust token limits, or tune generation behavior.
"""

from google.genai import types

# ============================================================================
# Model Selection
# ============================================================================

# Primary model for all agents. Options:
# - "gemini-flash-latest": Fast, cost-effective for most tasks
# - "gemini-pro-latest": More capable for complex reasoning
# - "gemini-ultra-latest": Highest quality (if available)
DEFAULT_MODEL = "gemini-flash-latest"

# Override for specific agent types (None = use DEFAULT_MODEL)
CLASSIFIER_MODEL = None  # Image classification
EXTRACTOR_MODEL = None   # Writing evidence extraction
GRAMMAR_MODEL = None     # Grammar training extraction


# ============================================================================
# Generation Configs
# ============================================================================

# Base config: deterministic output for reproducibility
BASE_GENERATION_CONFIG = types.GenerateContentConfig(
    temperature=0,
    seed=0,
)

# Classifier: fast, short output
CLASSIFIER_CONFIG = types.GenerateContentConfig(
    temperature=0,
    seed=0,
    max_output_tokens=4096,
)

# Writing extractor: longer output for transcription + model essay
EXTRACTOR_CONFIG = types.GenerateContentConfig(
    temperature=0,
    seed=0,
    # Caps runaway repetition loops; far above any legitimate essay
    # transcription + model essay + fixes, which stay under ~4k tokens.
    max_output_tokens=16384,
)

# Grammar extractor: moderate output for mistake lists
GRAMMAR_EXTRACTOR_CONFIG = types.GenerateContentConfig(
    temperature=0,
    seed=0,
    max_output_tokens=8192,
)


# ============================================================================
# Retry & Reliability
# ============================================================================

# Maximum attempts for processing one input image
PROCESS_INPUT_MAX_ATTEMPTS = 3

# Retry backoff: initial delay in seconds (exponential backoff applied)
RETRY_INITIAL_DELAY = 2


# ============================================================================
# Input/Output Paths
# ============================================================================

# These are imported from constants.py, but can be overridden here if needed
# WRITING_INPUTS_DIR = Path("./input")
# REPORTS_DIR = Path("./reports")
# TRAINING_INPUTS_DIR = Path("./training_inputs")
# PDF_EXPORTS_DIR = Path("./pdf_exports")


# ============================================================================
# Localization
# ============================================================================

# Default feedback language when not specified by user
from typing import Literal

FeedbackLanguage = Literal["zh-Hans", "en", "ja", "ko"]
DEFAULT_FEEDBACK_LANGUAGE: FeedbackLanguage = "zh-Hans"


# ============================================================================
# MIME Types
# ============================================================================

MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

SUFFIX_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}
