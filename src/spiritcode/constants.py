"""Developer-tunable values shared across the application."""

BASE_URL = "https://spiritlm.netlify.app"
DEV_BASE_URL = "http://localhost:3000"

API_PATHS = {
    "models": "/v1/models",
    "metadata": "/v1/spiritcode/models",
    "chat": "/v1/chat/completions",
    "profile": "/v1/account",
    "balance": "/v1/account/balance",
    "subscription": "/v1/account/subscription",
    "usage": "/v1/account/usage",
}

COLORS = {
    "accent": "#ffe02e",
    "text": "#f3f2ee",
    "muted": "#a5a5a1",
    "line": "#30302e",
    "success": "#8fca93",
    "error": "#ff6b6b",
    "info": "#91b9ff",
}

BACKGROUND = "#080808"

STAR = r"""       .-.
      /   \
  .--'     '--.
 /     | |     \
 \             /
  '--.     .--'
      \   /
       '-'"""

REQUEST_TIMEOUT = 100
ACCOUNT_TIMEOUT = 5
COMMAND_TIMEOUT = 120

MAX_ITERATIONS = 30
MAX_FILE_BYTES = 128_000
MAX_BATCH_EDITS = 20
MAX_RESULT_CHARS = 24_000
MAX_FILES = 2_000
MAX_SSE_EVENT = 1_048_576

# Deployment context window confirmed by the product owner. Server metadata takes precedence.
MODEL_CONTEXT_WINDOWS = {
    "anthropic/claude-haiku-5.5": 1_000_000,
    "claude-haiku-5.5": 1_000_000,
}

FALLBACK_CONTEXT = 16_000
DEFAULT_OUTPUT_TOKENS = 4_096
COMPACTION_THRESHOLD = 0.75

EXCLUDED = {".git", ".venv", "node_modules", "__pycache__", "dist", "build", ".next"}

SECRET_NAMES = {".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials"}
