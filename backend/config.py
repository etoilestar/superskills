from pathlib import Path
from typing import Optional
from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings
import os

# 获取环境变量
PROJECT_ROOT = Path(__file__).parent.parent
# 获取环境变量 LLM_BASE_URL
# 获取环境变量 DEFAULT_MODEL
default_model = os.getenv("DEFAULT_MODEL", "qwen3:32b")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434")


class Settings(BaseSettings):
    # LLM backend
    llm_base_url: str = Field(LLM_BASE_URL, validation_alias=AliasChoices("LLM_BASE_URL", "llm_base_url"))
    default_model: str = Field(default_model, validation_alias=AliasChoices("DEFAULT_MODEL", "default_model"))
    # Leave empty to use a local backend; set to your sk-... key for OpenAI.
    openai_api_key: Optional[str] = Field(None, validation_alias=AliasChoices("OPENAI_API_KEY", "openai_api_key"))
    # Alternative field name accepted by some deployments
    llm_api_key: Optional[str] = Field(None, validation_alias=AliasChoices("LLM_API_KEY", "llm_api_key"))
    # Optional key for image-generation backends when they differ from the LLM provider.
    image_api_key: Optional[str] = Field(None, validation_alias=AliasChoices("IMAGE_API_KEY", "image_api_key"))

    # Optional separate model for silent planner rounds (metadata / block / skill planner).
    # Falls back to default_model when unset.
    planner_model: Optional[str] = Field("qwen3:30b-instruct", validation_alias=AliasChoices("PLANNER_MODEL", "planner_model"))

    # Optional separate model used exclusively for output-format validation rounds
    # inside retry_with_validation().  A small/fast model is sufficient here because
    # validation only requires JSON-structured classification of a prior output.
    # Falls back to default_model when unset.
    validator_model: Optional[str] = Field("qwen3:8b", validation_alias=AliasChoices("VALIDATOR_MODEL", "validator_model"))

    # Optional capability-specific models. These allow the runtime to keep
    # action classification in code while SKILL.md only describes what to do.
    text_model: Optional[str] = Field("qwen3:30b", validation_alias=AliasChoices("TEXT_MODEL", "text_model"))
    code_model: Optional[str] = Field("qwen3-coder:30b", validation_alias=AliasChoices("CODE_MODEL", "code_model"))
    #image_model: Optional[str] = Field("qwen3-vl:32b", validation_alias=AliasChoices("IMAGE_MODEL", "image_model"))
    # Optional vision-language model for understanding uploaded images/screenshots.
    #vision_model: Optional[str] = Field("qwen3-vl:32b", validation_alias=AliasChoices("VISION_MODEL", "vision_model"))
    image_model: Optional[str] = Field(
        "stable-diffusion-2-1-base",
        validation_alias=AliasChoices("IMAGE_MODEL", "image_model"),
    )

    vision_model: Optional[str] = Field(
        "qwen3-vl:32b",
        validation_alias=AliasChoices("VISION_MODEL", "vision_model"),
    )

    image_base_url: str = Field(
        LLM_BASE_URL,
        validation_alias=AliasChoices("IMAGE_BASE_URL", "image_base_url"),
    )

    image_size: str = Field(
        "512x512",
        validation_alias=AliasChoices("IMAGE_SIZE", "image_size"),
    )
    # Optional JSON routing overrides, e.g.
    # {"tasks": {"code": "qwen-coder", "image": "sdxl", "vision": "qwen-vl"},
    #  "creator_paths": {"scripts/*": "code", "assets/*.png": "image"}}
    model_routing_json: Optional[str] = Field(None, validation_alias=AliasChoices("MODEL_ROUTING_JSON", "model_routing_json"))

    # Configurable heuristics used only for capability classification.
    code_file_extensions: str = Field(".py,.js,.mjs,.cjs,.ts,.tsx,.jsx,.sh,.bash,.rb,.go,.rs,.java,.c,.cpp,.cs,.php,.swift,.kt,.sql", validation_alias=AliasChoices("CODE_FILE_EXTENSIONS", "code_file_extensions"))
    image_task_keywords: str = Field("image,images,picture,pictures,photo,photos,logo,icon,illustration,draw,绘图,图片,图像,照片,海报,插画,图标,logo", validation_alias=AliasChoices("IMAGE_TASK_KEYWORDS", "image_task_keywords"))

    # When true, raise if the provider echoes a different response model.
    # Defaults to non-strict because many local OpenAI-compatible backends
    # canonicalize model IDs in streamed chunks.
    model_ack_strict: bool = Field(False, validation_alias=AliasChoices("MODEL_ACK_STRICT", "model_ack_strict"))

    # LLM generation parameters — omitted from request payload when unset so the
    # backend can apply its own defaults.
    temperature: Optional[float] = Field(None, validation_alias=AliasChoices("TEMPERATURE", "temperature"))
    max_tokens: Optional[int] = Field(None, validation_alias=AliasChoices("MAX_TOKENS", "max_tokens"))

    # Timeout for LLM HTTP requests in seconds.
    llm_timeout_seconds: int = Field(6000, validation_alias=AliasChoices("LLM_TIMEOUT_SECONDS", "llm_timeout_seconds"))

    # Filesystem paths
    kernel_path: Path = PROJECT_ROOT / "kernel"
    skills_path: Path = PROJECT_ROOT / "skills"
    managed_skills_path: Path = PROJECT_ROOT / "skills"
    workspace_skills_path: Path = PROJECT_ROOT / ".agents" / "skills"
    shared_skills_path: Path = Path.home() / ".agents" / "skills"
    bundled_skills_path: Path = PROJECT_ROOT / "bundled-skills"
    governance_path: Path = PROJECT_ROOT / ".skill-governance"

    # Resource reading limit per file (characters) used by read_skill_resource_text.
    skill_resource_max_chars: int = Field(20000, validation_alias=AliasChoices("SKILL_RESOURCE_MAX_CHARS", "skill_resource_max_chars"))

    # Maximum wall-clock seconds allowed for a single run_command subprocess.
    skill_command_timeout: int = Field(60, validation_alias=AliasChoices("SKILL_COMMAND_TIMEOUT", "skill_command_timeout"))

    model_config = {
        "env_file": PROJECT_ROOT / ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "populate_by_name": True,
    }

    @model_validator(mode="after")
    def _ensure_paths(self) -> "Settings":
        """Create skills_path if it does not exist; kernel_path must already exist."""
        self.managed_skills_path = self.skills_path
        self.managed_skills_path.mkdir(parents=True, exist_ok=True)
        self.workspace_skills_path.mkdir(parents=True, exist_ok=True)
        self.shared_skills_path.mkdir(parents=True, exist_ok=True)
        self.bundled_skills_path.mkdir(parents=True, exist_ok=True)
        self.governance_path.mkdir(parents=True, exist_ok=True)
        self.skills_path = self.managed_skills_path
        if not self.kernel_path.exists():
            raise ValueError(
                f"kernel_path does not exist: {self.kernel_path}. "
                "Ensure the kernel/ directory is present in the project root."
            )
        return self


settings = Settings()
