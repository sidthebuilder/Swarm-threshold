"""Peer-pressure threshold experiment harness: local scripted core, optional model layer."""

from .adaption import AdaptionTransport, select_transport
from .client import Completion, ModelClient, ModelSpec
from .config import DefenseConfig, ForumConfig, RunConfig
from .env import load_env_file, project_env_file
from .harness import RunRecord, run_agent
from .model_policy import make_model_cascade_policy, make_model_policy
from .prompts import PROMPT_VERSION

__all__ = [
    "AdaptionTransport",
    "Completion",
    "DefenseConfig",
    "ForumConfig",
    "ModelClient",
    "ModelSpec",
    "PROMPT_VERSION",
    "RunConfig",
    "RunRecord",
    "load_env_file",
    "make_model_cascade_policy",
    "make_model_policy",
    "project_env_file",
    "run_agent",
    "select_transport",
]
__version__ = "0.2.0"
